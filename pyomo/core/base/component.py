#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

import logging
import six
import sys
from copy import deepcopy
from pickle import PickleError
from six import iteritems, string_types
from weakref import ref as weakref_ref

from pyutilib.misc.indent_io import StreamIndenter

import pyomo.common
from pyomo.common.deprecation import deprecated, relocated_module_attribute
from pyomo.core.pyomoobject import PyomoObject
from pyomo.core.base.misc import tabular_writer, sorted_robust
from pyomo.core.base.indexed_component_slice import IndexedComponent_slice

logger = logging.getLogger('pyomo.core')

relocated_module_attribute(
    'ComponentUID', 'pyomo.core.base.componentuid.ComponentUID',
    version='TBD')

def _name_index_generator(idx):
    """
    Return a string representation of an index.
    """
    def _escape(val):
        if type(val) is tuple:
            ans = "(" + ','.join(_escape(_) for _ in val) + ")"
        else:
            # We need to quote set members (because people put things
            # like spaces - or worse commas - in their set names).  Our
            # plan is to put the strings in single quotes... but that
            # requires escaping any single quotes in the string... which
            # in turn requires escaping the escape character.
            ans = "%s" % (val,)
            if isinstance(val, six.string_types):
                ans = ans.replace("\\", "\\\\").replace("'", "\\'")
                if ',' in ans or "'" in ans:
                    ans = "'"+ans+"'"
        return ans
    if idx.__class__ is tuple:
        return "[" + ",".join(_escape(i) for i in idx) + "]"
    else:
        return "[" + _escape(idx) + "]"


def name(component, index=None, fully_qualified=False, relative_to=None):
    """
    Return a string representation of component for a specific
    index value.
    """
    base = component.getname(fully_qualified=fully_qualified, relative_to=relative_to)
    if index is None:
        return base
    else:
        if index not in component.index_set():
            raise KeyError( "Index %s is not valid for component %s"
                            % (index, component.name) )
        return base + _name_index_generator( index )


@deprecated(msg="The cname() function has been renamed to name()",
            version='5.6.9')
def cname(*args, **kwds):
    return name(*args, **kwds)


class CloneError(pyomo.common.errors.PyomoException):
    pass

class _ComponentBase(PyomoObject):
    """A base class for Component and ComponentData

    This class defines some fundamental methods and properties that are
    expected for all Component-like objects.  They are centralized here
    to avoid repeated code in the Component and ComponentData classes.
    """
    __slots__ = ()

    _PPRINT_INDENT = "    "

    def is_component_type(self):
        """Return True if this class is a Pyomo component"""
        return True

    def __deepcopy__(self, memo):
        # The problem we are addressing is when we want to clone a
        # sub-block in a model.  In that case, the block can have
        # references to both child components and to external
        # ComponentData (mostly through expressions pointing to Vars
        # and Params outside this block).  For everything stored beneath
        # this block, we want to clone the Component (and all
        # corresponding ComponentData objects).  But for everything
        # stored outside this Block, we want to do a simple shallow
        # copy.
        #
        # Nominally, expressions only point to ComponentData
        # derivatives.  However, with the development of Expression
        # Templates (and the corresponding _GetItemExpression object),
        # expressions can refer to container (non-Simple) components, so
        # we need to override __deepcopy__ for both Component and
        # ComponentData.

        #try:
        #    print("Component: %s" % (self.name,))
        #except:
        #    print("DANGLING ComponentData: %s on %s" % (
        #        type(self),self.parent_component()))

        # Note: there is an edge case when cloning a block: the initial
        # call to deepcopy (on the target block) has __block_scope__
        # defined, however, the parent block of self is either None, or
        # is (by definition) out of scope.  So we will check that
        # id(self) is not in __block_scope__: if it is, then this is the
        # top-level block and we need to do the normal deepcopy.
        if '__block_scope__' in memo and \
                id(self) not in memo['__block_scope__']:
            _known = memo['__block_scope__']
            _new = []
            tmp = self.parent_block()
            tmpId = id(tmp)
            # Note: normally we would need to check that tmp does not
            # end up being None.  However, since clone() inserts
            # id(None) into the __block_scope__ dictionary, we are safe
            while tmpId not in _known:
                _new.append(tmpId)
                tmp = tmp.parent_block()
                tmpId = id(tmp)

            # Remember whether all newly-encountered blocks are in or
            # out of scope (prevent duplicate work)
            for _id in _new:
                _known[_id] = _known[tmpId]

            if not _known[tmpId]:
                # component is out-of-scope.  shallow copy only
                ans = memo[id(self)] = self
                return ans

        #
        # There is a particularly subtle bug with 'uncopyable'
        # attributes: if the exception is thrown while copying a complex
        # data structure, we can be in a state where objects have been
        # created and assigned to the memo in the try block, but they
        # haven't had their state set yet.  When the exception moves us
        # into the except block, we need to effectively "undo" those
        # partially copied classes.  The only way is to restore the memo
        # to the state it was in before we started.  Right now, our
        # solution is to make a (shallow) copy of the memo before each
        # operation and restoring it in the case of exception.
        # Unfortunately that is a lot of usually unnecessary work.
        # Since *most* classes are copyable, we will avoid that
        # "paranoia" unless the naive clone generated an error - in
        # which case Block.clone() will switch over to the more
        # "paranoid" mode.
        #
        paranoid = memo.get('__paranoid__', None)

        ans = memo[id(self)] = self.__class__.__new__(self.__class__)
        # We can't do the "obvious", since this is a (partially)
        # slot-ized class and the __dict__ structure is
        # nonauthoritative:
        #
        # for key, val in self.__dict__.iteritems():
        #     object.__setattr__(ans, key, deepcopy(val, memo))
        #
        # Further, __slots__ is also nonauthoritative (this may be a
        # singleton component -- in which case it also has a __dict__).
        # Plus, as this may be a derived class with several layers of
        # slots.  So, we will resort to partially "pickling" the object,
        # deepcopying the state dict, and then restoring the copy into
        # the new instance.
        #
        # [JDS 7/7/14] I worry about the efficiency of using both
        # getstate/setstate *and* deepcopy, but we need deepcopy to
        # update the _parent refs appropriately, and since this is a
        # slot-ized class, we cannot overwrite the __deepcopy__
        # attribute to prevent infinite recursion.
        state = self.__getstate__()
        try:
            if paranoid:
                saved_memo = dict(memo)
            new_state = deepcopy(state, memo)
        except:
            if paranoid:
                # Note: memo is intentionally pass-by-reference.  We
                # need to clear and reset the object we were handed (and
                # not overwrite it)
                memo.clear()
                memo.update(saved_memo)
            elif paranoid is not None:
                raise PickleError()
            new_state = {}
            for k,v in iteritems(state):
                try:
                    if paranoid:
                        saved_memo = dict(memo)
                    new_state[k] = deepcopy(v, memo)
                except CloneError:
                    raise
                except:
                    if paranoid:
                        memo.clear()
                        memo.update(saved_memo)
                    elif paranoid is None:
                        logger.warning("""
                            Uncopyable field encountered when deep
                            copying outside the scope of Block.clone().
                            There is a distinct possibility that the new
                            copy is not complete.  To avoid this
                            situation, either use Block.clone() or set
                            'paranoid' mode by adding '__paranoid__' ==
                            True to the memo before calling
                            copy.deepcopy.""")
                    if self.model() is self:
                        what = 'Model'
                    else:
                        what = 'Component'
                    logger.error(
                        "Unable to clone Pyomo component attribute.\n"
                        "%s '%s' contains an uncopyable field '%s' (%s)"
                        % ( what, self.name, k, type(v) ))
                    # If this is an abstract model, then we are probably
                    # in the middle of create_instance, and the model
                    # that will eventually become the concrete model is
                    # missing initialization data.  This is an
                    # exceptional event worthy of a stronger (and more
                    # informative) error.
                    if not self.parent_component()._constructed:
                        raise CloneError(
                            "Uncopyable attribute (%s) encountered when "
                            "cloning component %s on an abstract block.  "
                            "The resulting instance is therefore "
                            "missing data from the original abstract model "
                            "and likely will not construct correctly.  "
                            "Consider changing how you initialize this "
                            "component or using a ConcreteModel."
                            % ( k, self.name ))
        ans.__setstate__(new_state)
        return ans

    @deprecated("""The cname() method has been renamed to getname().
    The preferred method of obtaining a component name is to use the
    .name property, which returns the fully qualified component name.
    The .local_name property will return the component name only within
    the context of the immediate parent container.""", version='5.0')
    def cname(self, *args, **kwds):
        return self.getname(*args, **kwds)

    def pprint(self, ostream=None, verbose=False, prefix=""):
        """Print component information

        Note that this method is generally only reachable through
        ComponentData objects in an IndexedComponent container.
        Components, including unindexed Component derivatives and both
        scalar and indexed IndexedComponent derivatives will see
        :py:meth:`Component.pprint()`
        """
        comp = self.parent_component()
        _attr, _data, _header, _fcn = comp._pprint()
        if isinstance(type(_data), six.string_types):
            # If the component _pprint only returned a pre-formatted
            # result, then we have no way to only emit the information
            # for this _data object.
            _name = comp.local_name
        else:
            # restrict output to only this data object
            _data = iter( ((self.index(), self),) )
            _name = "{Member of %s}" % (comp.local_name,)
        self._pprint_base_impl(
            ostream, verbose, prefix, _name, comp.doc,
            comp.is_constructed(), _attr, _data, _header, _fcn)

    @property
    def name(self):
        """Get the fully qualifed component name."""
        return self.getname(fully_qualified=True)

    # Adding a setter here to help users adapt to the new
    # setting. The .name attribute is now ._name. It should
    # never be assigned to by user code.
    @name.setter
    def name(self, val):
        raise ValueError(
            "The .name attribute is now a property method "
            "that returns the fully qualified component name. "
            "Assignment is not allowed.")

    @property
    def local_name(self):
        """Get the component name only within the context of
        the immediate parent container."""
        return self.getname(fully_qualified=False)

    @property
    def active(self):
        """Return the active attribute"""
        # Normal components cannot be deactivated
        return True

    @active.setter
    def active(self, value):
        """Set the active attribute to the given value"""
        raise AttributeError(
            "Setting the 'active' flag on a component that does not "
            "support deactivation is not allowed.")

    def _pprint_base_impl(self, ostream, verbose, prefix, _name, _doc,
                          _constructed, _attr, _data, _header, _fcn):
        if ostream is None:
            ostream = sys.stdout
        if prefix:
            ostream = StreamIndenter(ostream, prefix)

        # FIXME: HACK for backwards compatability with suppressing the
        # header for the top block
        if not _attr and self.parent_block() is None:
            _name = ''

        # We only indent everything if we printed the header
        if _attr or _name or _doc:
            ostream = StreamIndenter(ostream, self._PPRINT_INDENT)
            # The first line should be a hanging indent (i.e., not indented)
            ostream.newline = False

        if _name:
            ostream.write(_name+" : ")
        if _doc:
            ostream.write(_doc+'\n')
        if _attr:
            ostream.write(", ".join("%s=%s" % (k,v) for k,v in _attr))
        if _attr or _name or _doc:
            ostream.write("\n")

        if not _constructed:
            # HACK: for backwards compatability, Abstract blocks will
            # still print their assigned components.  Should we instead
            # always pprint unconstructed components (possibly
            # suppressing the table header if the table is empty)?
            if self.parent_block() is not None:
                ostream.write("Not constructed\n")
                return

        if type(_fcn) is tuple:
            _fcn, _fcn2 = _fcn
        else:
            _fcn2 = None

        if _header is not None:
            if _fcn2 is not None:
                _data_dict = dict(_data)
                _data = iteritems(_data_dict)
            tabular_writer( ostream, '', _data, _header, _fcn )
            if _fcn2 is not None:
                for _key in sorted_robust(_data_dict):
                    _fcn2(ostream, _key, _data_dict[_key])
        elif _fcn is not None:
            _data_dict = dict(_data)
            for _key in sorted_robust(_data_dict):
                _fcn(ostream, _key, _data_dict[_key])
        elif _data is not None:
            ostream.write(_data)


class Component(_ComponentBase):
    """
    This is the base class for all Pyomo modeling components.

    Constructor arguments:
        ctype           The class type for the derived subclass
        doc             A text string describing this component
        name            A name for this component

    Public class attributes:
        doc             A text string describing this component

    Private class attributes:
        _constructed    A boolean that is true if this component has been
                            constructed
        _parent         A weakref to the parent block that owns this component
        _ctype          The class type for the derived subclass
    """

    def __init__ (self, **kwds):
        #
        # Get arguments
        #
        self._ctype = kwds.pop('ctype', None)
        self.doc    = kwds.pop('doc', None)
        self._name  = kwds.pop('name', str(type(self).__name__))
        if kwds:
            raise ValueError(
                "Unexpected keyword options found while constructing '%s':\n\t%s"
                % ( type(self).__name__, ','.join(sorted(kwds.keys())) ))
        #
        # Verify that ctype has been specified.
        #
        if self._ctype is None:
            raise pyomo.common.DeveloperError(
                "Must specify a component type for class %s!"
                % ( type(self).__name__, ) )
        #
        self._constructed   = False
        self._parent        = None    # Must be a weakref

    def __getstate__(self):
        """
        This method must be defined to support pickling because this class
        owns weakrefs for '_parent'.
        """
        #
        # Nominally, __getstate__() should return:
        #
        # state = super(Class, self).__getstate__()
        # for i in Class.__dict__:
        #     state[i] = getattr(self,i)
        # return state
        #
        # However, in this case, the (nominal) parent class is 'object',
        # and object does not implement __getstate__.  So, we will check
        # to make sure that there is a base __getstate__() to call...
        #
        _base = super(Component,self)
        if hasattr(_base, '__getstate__'):
            state = _base.__getstate__()
            for key,val in iteritems(self.__dict__):
                if key not in state:
                    state[key] = val
        else:
            state = dict(self.__dict__)
        if self._parent is not None:
            state['_parent'] = self._parent()
        return state

    def __setstate__(self, state):
        """
        This method must be defined to support pickling because this class
        owns weakrefs for '_parent'.
        """
        if state['_parent'] is not None and \
                type(state['_parent']) is not weakref_ref:
            state['_parent'] = weakref_ref(state['_parent'])
        #
        # Note: our model for setstate is for derived classes to modify
        # the state dictionary as control passes up the inheritance
        # hierarchy (using super() calls).  All assignment of state ->
        # object attributes is handled at the last class before 'object'
        # (which may -- or may not (thanks to MRO) -- be here.
        #
        _base = super(Component,self)
        if hasattr(_base, '__setstate__'):
            _base.__setstate__(state)
        else:
            for key, val in iteritems(state):
                # Note: per the Python data model docs, we explicitly
                # set the attribute using object.__setattr__() instead
                # of setting self.__dict__[key] = val.
                object.__setattr__(self, key, val)

    @property
    def ctype(self):
        """Return the class type for this component"""
        return self._ctype

    @deprecated("Component.type() method has been replaced by the "
                ".ctype property.", version='5.7')
    def type(self):
        """Return the class type for this component"""
        return self.ctype

    def construct(self, data=None):                     #pragma:nocover
        """API definition for constructing components"""
        pass

    def is_constructed(self):                           #pragma:nocover
        """Return True if this class has been constructed"""
        return self._constructed

    def reconstruct(self, data=None):
        """Re-construct model expressions"""
        self._constructed = False
        self.construct(data=data)

    def valid_model_component(self):
        """Return True if this can be used as a model component."""
        return True

    def pprint(self, ostream=None, verbose=False, prefix=""):
        """Print component information"""
        self._pprint_base_impl(
            ostream, verbose, prefix, self.local_name, self.doc,
            self.is_constructed(), *self._pprint()
        )

    def display(self, ostream=None, verbose=False, prefix=""):
        self.pprint(ostream=ostream, prefix=prefix)

    def parent_component(self):
        """Returns the component associated with this object."""
        return self

    def parent_block(self):
        """Returns the parent of this object."""
        if self._parent is None:
            return None
        else:
            return self._parent()

    def model(self):
        """Returns the model associated with this object."""
        # This is a re-implementation of Component.parent_block(),
        # duplicated for effficiency to avoid the method call
        if self._parent is None:
            return None
        ans = self._parent()

        if ans is None:
            return None
        # Defer to the (simple) block's model() method to walk up the
        # hierarchy. This is because the top-level block can be a model,
        # but nothing else (e.g., calling model() on a Var not attached
        # to a model should return None, but calling model() on a Block
        # not attached to anything else should return the Block)
        return ans.model()

    def root_block(self):
        """Return self.model()"""
        return self.model()

    def __str__(self):
        """Return the component name"""
        return self.name

    def to_string(self, verbose=None, labeler=None, smap=None, compute_values=False):
        """Return the component name"""
        if compute_values:
            try:
                return str(self())
            except:
                pass
        return self.name

    def getname(self, fully_qualified=False, name_buffer=None, relative_to=None):
        """Returns the component name associated with this object.

        Parameters
        ----------
        fully_qualified: bool
            Generate full name from nested block names

        name_buffer: dict
            A dictionary that caches encountered names and indices.
            Providing a ``name_buffer`` can significantly speed up
            iterative name generation

        relative_to: Block
            Generate fully_qualified names reletive to the specified block.
        """
        if fully_qualified:
            pb = self.parent_block()
            if relative_to is None:
                relative_to = self.model()
            if pb is not None and pb is not relative_to:
                ans = pb.getname(fully_qualified, name_buffer, relative_to) \
                      + "." + self._name
            elif pb is None and relative_to != self.model():
                raise RuntimeError(
                    "The relative_to argument was specified but not found "
                    "in the block hierarchy: %s" % str(relative_to))
            else:
                ans = self._name
        else:
            ans = self._name
        if name_buffer is not None:
            name_buffer[id(self)] = ans
        return ans

    @property
    def name(self):
        """Get the fully qualifed component name."""
        return self.getname(fully_qualified=True)

    # Allow setting a componet's name if it is not owned by a parent
    # block (this supports, e.g., naming a model)
    @name.setter
    def name(self, val):
        if self.parent_block() is None:
            self._name = val
        else:
            raise ValueError(
                "The .name attribute is not settable when the component "
                "is assigned to a Block.\nTriggered by attempting to set "
                "component '%s' to name '%s'" % (self.name,val))

    def is_indexed(self):
        """Return true if this component is indexed"""
        return False

    def clear_suffix_value(self, suffix_or_name, expand=True):
        """Clear the suffix value for this component data"""
        if isinstance(suffix_or_name, six.string_types):
            import pyomo.core.base.suffix
            for name_, suffix_ in pyomo.core.base.suffix.active_suffix_generator(self.model()):
                if suffix_or_name == name_:
                    suffix_.clear_value(self, expand=expand)
                    break
        else:
            suffix_or_name.clear_value(self, expand=expand)

    def set_suffix_value(self, suffix_or_name, value, expand=True):
        """Set the suffix value for this component data"""
        if isinstance(suffix_or_name, six.string_types):
            import pyomo.core.base.suffix
            for name_, suffix_ in pyomo.core.base.suffix.active_suffix_generator(self.model()):
                if suffix_or_name == name_:
                    suffix_.set_value(self, value, expand=expand)
                    break
        else:
            suffix_or_name.set_value(self, value, expand=expand)

    def get_suffix_value(self, suffix_or_name, default=None):
        """Get the suffix value for this component data"""
        if isinstance(suffix_or_name, six.string_types):
            import pyomo.core.base.suffix
            for name_, suffix_ in pyomo.core.base.suffix.active_suffix_generator(self.model()):
                if suffix_or_name == name_:
                    return suffix_.get(self, default)
        else:
            return suffix_or_name.get(self, default)


class ActiveComponent(Component):
    """A Component that makes semantic sense to activate or deactivate
    in a model.

    Private class attributes:
        _active         A boolean that is true if this component will be
                            used in model operations
    """

    def __init__(self, **kwds):
        self._active = True
        super(ActiveComponent, self).__init__(**kwds)

    @property
    def active(self):
        """Return the active attribute"""
        return self._active

    @active.setter
    def active(self, value):
        """Set the active attribute to the given value"""
        raise AttributeError(
            "Assignment not allowed. Use the (de)activate methods." )

    def activate(self):
        """Set the active attribute to True"""
        self._active=True

    def deactivate(self):
        """Set the active attribute to False"""
        self._active=False


class ComponentData(_ComponentBase):
    """
    This is the base class for the component data used
    in Pyomo modeling components.  Subclasses of ComponentData are
    used in indexed components, and this class assumes that indexed
    components are subclasses of IndexedComponent.  Note that
    ComponentData instances do not store their index.  This makes
    some operations significantly more expensive, but these are (a)
    associated with I/O generation and (b) this cost can be managed
    with caches.

    Constructor arguments:
        owner           The component that owns this data object

    Private class attributes:
        _component      A weakref to the component that owns this data object
        """

    __pickle_slots__ = ('_component',)
    __slots__ = __pickle_slots__ + ('__weakref__',)

    def __init__(self, component):
        #
        # ComponentData objects are typically *private* objects for
        # indexed / sparse indexed components.  As such, the (derived)
        # class needs to make sure that the owning component is *always*
        # passed as the owner (and that owner is never None).  Not validating
        # this assumption is significantly faster.
        #
        self._component = weakref_ref(component)

    def __getstate__(self):
        """Prepare a picklable state of this instance for pickling.

        Nominally, __getstate__() should return:

            state = super(Class, self).__getstate__()
            for i in Class.__slots__:
                state[i] = getattr(self,i)
            return state

        However, in this case, the (nominal) parent class is 'object',
        and object does not implement __getstate__.  So, we will check
        to make sure that there is a base __getstate__() to call...
        You might think that there is nothing to check, but multiple
        inheritance could mean that another class got stuck between
        this class and "object" in the MRO.

        This method must be defined to support pickling because this
        class owns weakrefs for '_component', which must be either
        removed or converted to hard references prior to pickling.

        Further, since there is only a single slot, and that slot
        (_component) requires special processing, we will just deal with
        it explicitly.  As _component is a weakref (not pickable), we
        need to resolve it to a concrete object.
        """
        _base = super(ComponentData,self)
        if hasattr(_base, '__getstate__'):
            state = _base.__getstate__()
        else:
            state = {}
        #
        if self._component is None:
            state['_component'] = None
        else:
            state['_component'] = self._component()
        return state

    def __setstate__(self, state):
        """Restore a pickled state into this instance

        Note: our model for setstate is for derived classes to modify
        the state dictionary as control passes up the inheritance
        hierarchy (using super() calls).  All assignment of state ->
        object attributes is handled at the last class before 'object'
        (which may -- or may not (thanks to MRO) -- be here.

        This method must be defined to support unpickling because this
        class owns weakrefs for '_component', which must be restored
        from the hard references used in the piclke.
        """
        #
        # FIXME: We shouldn't have to check for weakref.ref here, but if
        # we don't the model cloning appears to fail (in the Benders
        # example)
        #
        if state['_component'] is not None and \
                type(state['_component']) is not weakref_ref:
            state['_component'] = weakref_ref(state['_component'])
        #
        # Note: our model for setstate is for derived classes to modify
        # the state dictionary as control passes up the inheritance
        # hierarchy (using super() calls).  All assignment of state ->
        # object attributes is handled at the last class before 'object'
        # (which may -- or may not (thanks to MRO) -- be here.
        #
        _base = super(ComponentData,self)
        if hasattr(_base, '__setstate__'):
            _base.__setstate__(state)
        else:
            for key, val in iteritems(state):
                # Note: per the Python data model docs, we explicitly
                # set the attribute using object.__setattr__() instead
                # of setting self.__dict__[key] = val.
                object.__setattr__(self, key, val)

    @property
    def ctype(self):
        """Return the class type for this component"""
        _parent = self.parent_component()
        if _parent is None:
            return None
        return _parent._ctype

    @deprecated("Component.type() method has been replaced by the "
                ".ctype property.", version='5.7')
    def type(self):
        """Return the class type for this component"""
        return self.ctype

    def parent_component(self):
        """Returns the component associated with this object."""
        if self._component is None:
            return None
        return self._component()

    def parent_block(self):
        """Return the parent of the component that owns this data. """
        # This is a re-implementation of parent_component(), duplicated
        # for effficiency to avoid the method call
        if self._component is None:
            return None
        comp = self._component()

        # This is a re-implementation of Component.parent_block(),
        # duplicated for effficiency to avoid the method call
        if comp._parent is None:
            return None
        return comp._parent()

    def model(self):
        """Return the model of the component that owns this data. """
        ans = self.parent_block()
        if ans is None:
            return None
        # Defer to the (simple) block's model() method to walk up the
        # hierarchy. This is because the top-level block can be a model,
        # but nothing else (e.g., calling model() on a Var not attached
        # to a model should return None, but calling model() on a Block
        # not attached to anything else should return the Block)
        return ans.model()

    def index(self):
        """
        Returns the index of this ComponentData instance relative
        to the parent component index set. None is returned if
        this instance does not have a parent component, or if
        - for some unknown reason - this instance does not belong
        to the parent component's index set. This method is not
        intended to be a fast method;  it should be used rarely,
        primarily in cases of label formulation.
        """
        self_component = self.parent_component()
        if self_component is None:
            return None
        for idx, component_data in self_component.iteritems():
            if component_data is self:
                return idx
        return None

    def __str__(self):
        """Return a string with the component name and index"""
        return self.name

    def to_string(self, verbose=None, labeler=None, smap=None, compute_values=False):
        """
        Return a string representation of this component,
        applying the labeler if passed one.
        """
        if compute_values:
            try:
                return str(self())
            except:
                pass
        if smap:
            return smap.getSymbol(self, labeler)
        if labeler is not None:
            return labeler(self)
        else:
            return self.__str__()

    def getname(self, fully_qualified=False, name_buffer=None, relative_to=None):
        """Return a string with the component name and index"""
        #
        # Using the buffer, which is a dictionary:  id -> string
        #
        if name_buffer is not None and id(self) in name_buffer:
            # Return the name if it is in the buffer
            return name_buffer[id(self)]

        c = self.parent_component()
        if c is self:
            #
            # This is a scalar component, so call the
            # Component.getname() method
            #
            return super(ComponentData, self).getname(
                fully_qualified, name_buffer, relative_to)
        elif c is not None:
            #
            # Get the name of the parent component
            #
            base = c.getname(fully_qualified, name_buffer, relative_to)
        else:
            #
            # Defensive: this is a ComponentData without a valid
            # parent_component.  As this usually occurs when handling
            # exceptions during model construction, we need to ensure
            # that this method doesn't itself raise another exception.
            #
            return '[Unattached %s]' % (type(self).__name__,)

        if name_buffer is not None:
            # Iterate through the dictionary and generate all names in
            # the buffer
            for idx, obj in iteritems(c):
                name_buffer[id(obj)] = base + _name_index_generator(idx)
            if id(self) in name_buffer:
                # Return the name if it is in the buffer
                return name_buffer[id(self)]
        else:
            #
            # No buffer, so we iterate through the component _data
            # dictionary until we find this object.  This can be much
            # more expensive than if a buffer is provided.
            #
            for idx, obj in iteritems(c):
                if obj is self:
                    return base + _name_index_generator(idx)
        #
        raise RuntimeError("Fatal error: cannot find the component data in "
                           "the owning component's _data dictionary.")

    def is_indexed(self):
        """Return true if this component is indexed"""
        return False

    def clear_suffix_value(self, suffix_or_name, expand=True):
        """Set the suffix value for this component data"""
        if isinstance(suffix_or_name, six.string_types):
            import pyomo.core.base.suffix
            for name_, suffix_ in pyomo.core.base.suffix.active_suffix_generator(self.model()):
                if suffix_or_name == name_:
                    suffix_.clear_value(self, expand=expand)
                    break
        else:
            suffix_or_name.clear_value(self, expand=expand)

    def set_suffix_value(self, suffix_or_name, value, expand=True):
        """Set the suffix value for this component data"""
        if isinstance(suffix_or_name, six.string_types):
            import pyomo.core.base.suffix
            for name_, suffix_ in pyomo.core.base.suffix.active_suffix_generator(self.model()):
                if suffix_or_name == name_:
                    suffix_.set_value(self, value, expand=expand)
                    break
        else:
            suffix_or_name.set_value(self, value, expand=expand)

    def get_suffix_value(self, suffix_or_name, default=None):
        """Get the suffix value for this component data"""
        if isinstance(suffix_or_name, six.string_types):
            import pyomo.core.base.suffix
            for name_, suffix_ in pyomo.core.base.suffix.active_suffix_generator(self.model()):
                if suffix_or_name == name_:
                    return suffix_.get(self, default)
        else:
            return suffix_or_name.get(self, default)


class ActiveComponentData(ComponentData):
    """
    This is the base class for the component data used
    in Pyomo modeling components that can be activated and
    deactivated.

    It's possible to end up in a state where the parent Component
    has _active=True but all ComponentData have _active=False. This
    seems like a reasonable state, though we cannot easily detect
    this situation.  The important thing to avoid is the situation
    where one or more ComponentData are active, but the parent
    Component claims active=False. This class structure is designed
    to prevent this situation.

    Constructor arguments:
        owner           The component that owns this data object

    Private class attributes:
        _component      A weakref to the component that owns this data object
        _active         A boolean that indicates whether this data is active
    """

    __slots__ = ( '_active', )

    def __init__(self, component):
        super(ActiveComponentData, self).__init__(component)
        self._active = True

    def __getstate__(self):
        """
        This method must be defined because this class uses slots.
        """
        result = super(ActiveComponentData, self).__getstate__()
        for i in ActiveComponentData.__slots__:
            result[i] = getattr(self, i)
        return result

    # Since this class requires no special processing of the state
    # dictionary, it does not need to implement __setstate__()

    @property
    def active(self):
        """Return the active attribute"""
        return self._active

    @active.setter
    def active(self, value):
        """Set the active attribute to a specified value."""
        raise AttributeError(
            "Assignment not allowed. Use the (de)activate method" )

    def activate(self):
        """Set the active attribute to True"""
        self._active = self.parent_component()._active = True

    def deactivate(self):
        """Set the active attribute to False"""
        self._active = False

class ComponentUID(object):
    """
    This class provides a system to generate "component unique
    identifiers".  Any component in a model can be described by a CUID,
    and from a CUID you can find the component.  An important feature of
    CUIDs is that they are relative to a model, so you can use a CUID
    generated on one model to find the equivalent component on another
    model.  This is especially useful when you clone a model and want
    to, for example, copy a variable value from the cloned model back to
    the original model.

    The CUID has a string representation that can specify a specific
    component or a group of related components through the use of index
    wildcards (* for a single element in the index, and ** for all
    indexes)

    This class is also used by test_component.py to validate the structure
    of components.
    """

    __slots__ = ( '_cids', )
    tList = [ int, str ]
    tKeys = '#$'
    tDict = {} # ...initialized below

    def __init__(self, component, cuid_buffer=None, context=None):
        # A CUID can be initialized from either a reference component or
        # the string representation.
        if isinstance(component, string_types):
            if context is not None:
                raise ValueError("Context is not allowed when initializing a "
                                 "ComponentUID object from a string type")
            self._cids = tuple(self.parse_cuid(component))
        elif type(component) is IndexedComponent_slice:
            self._cids = tuple(self._generate_cids_from_slice(
                component,
                context=context,
                ))
        else:
            self._cids = tuple(self._generate_cuid(component,
                                                   cuid_buffer=cuid_buffer,
                                                   context=context))

    def __str__(self):
        """
        TODO
        """
        a = ""
        for name, args, types in reversed(self._cids):
            if a:
                a += '.' + name
            else:
                a = name
            if types is None:
                a += '[**]'
                continue
            if len(args) == 0:
                continue
            a += '['+','.join(str(x) or '*' for x in args) + ']'
        return a

    def __repr__(self):
        """
        TODO
        """
        a = ""
        for name, args, types in reversed(self._cids):
            if a:
                a += '.' + name
            else:
                a = name
            if types is None:
                a += ':**'
                continue
            if len(args) == 0:
                continue
            a += ':'+','.join( (types[i] if types[i] not in '.' else '')+str(x)
                               for i,x in enumerate(args) )
        return a

    def __getstate__(self):
        return {x:getattr(self, x) for x in ComponentUID.__slots__}

    def __setstate__(self, state):
        for key, val in iteritems(state):
            setattr(self,key,val)

    # Define all comparison operators using the underlying tuple's
    # comparison operators. We will be lazy and assume that the other is
    # a CUID.

    def __hash__(self):
        """
        TODO
        """
        return self._cids.__hash__()

    def __lt__(self, other):
        """
        TODO
        """
        try:
            return self._cids.__lt__(other._cids)
        except AttributeError:
            return self._cids.__lt__(other)

    def __le__(self, other):
        """
        TODO
        """
        try:
            return self._cids.__le__(other._cids)
        except AttributeError:
            return self._cids.__le__(other)

    def __gt__(self, other):
        """
        TODO
        """
        try:
            return self._cids.__gt__(other._cids)
        except AttributeError:
            return self._cids.__gt__(other)

    def __ge__(self, other):
        """
        TODO
        """
        try:
            return self._cids.__ge__(other._cids)
        except AttributeError:
            return self._cids.__ge__(other)

    def __eq__(self, other):
        """
        TODO
        """
        try:
            return self._cids.__eq__(other._cids)
        except AttributeError:
            return self._cids.__eq__(other)

    def __ne__(self, other):
        """
        TODO
        """
        try:
            return self._cids.__ne__(other._cids)
        except AttributeError:
            return self._cids.__ne__(other)

    def _validate_slice_index(self, idx):
        """
        Slices support a wider range of indices than CUIDs.
        This method makes sure the index can be used to
        construct a valid CUID, and returns an index in
        the form expected by _partial_cuid_from_index.
        """
        if idx.__class__ is not tuple:
            idx = (idx,)
        ellipsis_count = 0
        slice_count = 0
        fixed_count = 0
        validated_index = []
        for v in idx:
            if type(v) == slice:
                validated_index.append(v)
                slice_count += 1
            elif v == Ellipsis:
                if ellipsis_count:
                    raise NotImplementedError(
                        "Got invalid index %s when creating CUID. "
                        "Multiple ellipses are not supported." % (idx,)
                        )
                if fixed_count:
                    raise NotImplementedError(
                        "Got invalid index %s when creating CUID. "
                        "Fixed indices are not supported in the same "
                        "index as an ellipsis." % (idx,)
                        )
                validated_index.append('**')
                ellipsis_count += 1
            else:
                if ellipsis_count:
                    raise NotImplementedError(
                        "Got invalid index %s when creating CUID. "
                        "Fixed indices are not supported in the same "
                        "index as an ellipsis." % (idx,)
                        )
                validated_index.append(v)
                fixed_count += 1
        if ellipsis_count:
            # Only ellipses and slices should be present.
            # CUIDs expect a "blank-check" wildcard to be
            # present by itself.
            return (Ellipsis,)
        else:
            # Fixed and sliced indices can be followed by
            # _partial_cuid_from_index.
            return tuple(validated_index)

    def _partial_cuid_from_index(self, idx):
        """
        Converts an index into an (index, type-string) tuple
        as required by _cid entries. Slices and ellipses are
        converted to wildcard notation.
        """
        tDict = ComponentUID.tDict
        if idx.__class__ is not tuple:
            idx = (idx,)
        if idx == (Ellipsis,):
            # We assume a possible slice index has been validated 
            # so that this is the only way an ellipsis can be present.
            #
            # The CUID convention is to not store '**' in a tuple.
            # I.e. the _cid entry contains '**' rather than ('**',).
            return ( '**', None )
        return ( 
                # CUID convention for a wildcard:

                # Empty string in the index tuple
                tuple(
                    x if type(x) is not slice else ''
                    for x in idx
                    ),

                # '*' in the type string
                ''.join(
                    tDict.get(type(x), '?')
                    if type(x) is not slice else '*'
                    for x in idx
                    ),
                )

    def _index_from_slice_info(self, slice_info):
        """
        Constructs an index from the slice_info entry in a slice's
        call stack. The index may then be processed just as any
        other slice index, e.g. from a __getitem__ call in a slice's
        call stack.
        """
        fixed, sliced, ellipsis = slice_info
        
        if ellipsis is None:
            ellipsis = {}
        else:
            ellipsis = {ellipsis: Ellipsis}

        value_map = {}
        value_map.update(fixed)
        try:
            value_map.update(sliced)
        except TypeError:
            sliced = {i: slice(None) for i in sliced}
            value_map.update(sliced)
        value_map.update(ellipsis)


        # Assume that the keys of fixed, sliced, and ellipsis
        # partition the index we're describing.
        return tuple( value_map[i] for i in range(len(value_map)) )

    def _partial_cuid_from_slice_info(self, slice_info):
        """
        Gets an index from the slice_info entry in a slice's
        call stack, then validates that index. (Only a subset
        of the "indices" supported by a slice are supported
        by ComponentUIDs.) The slice_info entry is parsed
        separately because it has a very different structure 
        than the index in a get_item entry.
        """
        index = self._index_from_slice_info(slice_info)
        validated_index = self._validate_slice_index(index)
        return self._partial_cuid_from_index(validated_index)

    def _generate_cids_from_slice(self, _slice, context=None):
        """
        Pops the slice's call stack, generating a _cid entry whenever
        a __getattr__ call is encountered.
        """
        call_stack = list(_slice._call_stack)
        index = ()
        name = None
        count = 0
        while call_stack:
            # Pop the call stack, then do one of the following depending
            # on the call that was just popped:
            # - slice_info: generate cids by walking up model hierarchy
            # - call or get_item: cache arguments, which will be yielded
            #                     the next time a get_attribute is 
            #                     encountered
            # - get_attribute: yield a cid with the attribute and the
            #                  cached index value
            call_stack_entry = call_stack.pop()
            try:
                call, arg = call_stack_entry
            except ValueError as err:
                call, arg, kwds = call_stack_entry
            count += 1

            if call & 0b10:
                # Least significant bits in `set` and `del` calls
                # are 0b10 and 0b11. These are hardcoded into the
                # IndexedComponent_slice class.
                raise ValueError(
                    "Cannot create a CUID from a slice that "
                    "contains `set` or `del` calls. Got call %s "
                    "with argument '%s'" % (call, arg)
                    )
            elif call == IndexedComponent_slice.slice_info:
                # This should be the base of the stack.
                comp = arg[0]
                slice_info = arg[1:]
                yield (
                        (comp.local_name,) + 
                        self._partial_cuid_from_slice_info(slice_info)
                        )
                parent = comp.parent_block()
                for cid in self._generate_cuid(parent, context=context):
                    # Generate _cid entries for parent (non-slice)
                    # components. This is the only place `context` gets
                    # used, as the slice does not access any "components"
                    # in its call stack.
                    yield cid
            elif call == IndexedComponent_slice.get_item:
                # Need to parse index to get potential slice
                index = arg
                # Note that this assumes we will never have two get_item
                # calls in a row.
            elif call == IndexedComponent_slice.call:
                if len(arg) != 1:
                    raise NotImplementedError(
                            "Cannot create a CUID from a slice with a "
                            "call that has multiple arguments. Got "
                            "arguments %s." % (arg,)
                            )
                # Cache argument of a call to `component`
                name = arg[0]
                if kwds != {}:
                    raise NotImplementedError(
                            "Cannot create a CUID from a slice with a "
                            "call that contains keywords. Got keyword "
                            "dict %s." % (kwds,)
                            )
            elif call == IndexedComponent_slice.get_attribute:
                if name is not None:
                    # This only happens if IndexedComponent_slice.call
                    # was encountered.
                    if arg != 'component':
                        raise NotImplementedError(
                            "Cannot create a CUID from a slice with a "
                            "call to any method other than `component`. "
                            "Got %s." % arg
                            )
                    else:
                        # name is the attr we actually want to get
                        arg = name
                        # Reset name to None
                        name = None
                #if count == 1:
                     # We have encountered a get_attr at the top
                     # of our stack. This has been handled differently
                     # depending on how the CUID was constructed:
                     # - Constructing a CUID from an indexed component
                     #   will treat its indices as wildcards.
                     # - Constructing from a string will yield a valid
                     #   CUID that will treat the indexed component as
                     #   itself (rather than a slice over its data).
                     # This is an issue with CUID that should be
                     # addressed.
                     #
                     # The slice has no way to know whether the
                     # component is indexed, so we follow the same
                     # convention as constructing from a string to
                     # avoid attaching a wildcard to an unindexed
                     # component.
                     # This also simplifies the code and removes the
                     # need for an if tree.
                #     yield (arg, '**', None)

                # Preprocess and validate potential slice/Ellipsis index
                index = self._validate_slice_index(index)
                yield (arg,) + self._partial_cuid_from_index(index)
                # Reset index to empty tuple (the CUID convention for a
                # simple component)
                index = ()

    def _generate_cuid(self, component, cuid_buffer=None, context=None):
        """
        TODO
        """
        model = component.model()
        if context is None:
            context = model
        orig_component = component
        tDict = ComponentUID.tDict
        if not hasattr(component, '_component'):
            yield ( component.local_name, '**', None )
            component = component.parent_block()
        while component is not context:
            if component is model:
                raise ValueError("Context '%s' does not apply to component "
                                 "'%s'" % (context.name,
                                           orig_component.name))
            c = component.parent_component()
            if c is component:
                yield ( c.local_name, tuple(), '' )
            elif cuid_buffer is not None:
                if id(self) not in cuid_buffer:
                    for idx, obj in iteritems(c):
                        cuid_buffer[id(obj)] = \
                            self._partial_cuid_from_index(idx)
                yield (c.local_name,) + cuid_buffer[id(component)]
            else:
                for idx, obj in iteritems(c):
                    if obj is component:
                        yield (c.local_name,) + self._partial_cuid_from_index(idx)
                        break
            component = component.parent_block()

    def parse_cuid(self, label):
        """
        TODO
        """
        cList = label.split('.')
        # NOTE: This split is not safe for labels that include
        # decimal indices.
        tKeys = ComponentUID.tKeys
        tDict = ComponentUID.tDict
        for c in reversed(cList):
            if c[-1] == ']':
                c_info = c[:-1].split('[',1)
            else:
                c_info = c.split(':',1)
            if len(c_info) == 1:
                yield ( c_info[0], tuple(), '' )
            else:
                idx = c_info[1].split(',')
                _type = ''
                for i, val in enumerate(idx):
                    if val == '*':
                        _type += '*'
                        idx[i] = ''
                    elif val[0] in tKeys:
                        _type += val[0]
                        idx[i] = tDict[val[0]](val[1:])
                    elif val[0] in  "\"'" and val[-1] == val[0]:
                        _type += ComponentUID.tDict[str]
                        idx[i] = val[1:-1]
                    else:
                        _type += '.'
                if len(idx) == 1 and idx[0] == '**':
                    yield ( c_info[0], '**', None )
                else:
                    yield ( c_info[0], tuple(idx), _type )

    def find_component_on(self, block):
        """
        TODO
        """
        return self.find_component(block)

    def find_component(self, block):
        """
        Return the (unique) component in the block.  If the CUID contains
        a wildcard in the last component, then returns that component.  If
        there are wildcards elsewhere (or the last component was a partial
        slice), then returns None.  See list_components below.
        """
        obj = block
        for name, idx, types in reversed(self._cids):
            try:
                if len(idx) and idx != '**' and types.strip('*'):
                    obj = getattr(obj, name)[idx]
                else:
                    obj = getattr(obj, name)
            except KeyError:
                if '.' not in types:
                    return None
                tList = ComponentUID.tList
                def _checkIntArgs(_idx, _t, _i):
                    if _i == -1:
                        try:
                            return getattr(obj, name)[tuple(_idx)]
                        except KeyError:
                            return None
                    _orig = _idx[_i]
                    for _cast in tList:
                        try:
                            _idx[_i] = _cast(_orig)
                            ans = _checkIntArgs(_idx, _t, _t.find('.',_i+1))
                            if ans is not None:
                                return ans
                        except ValueError:
                            pass
                    _idx[_i] = _orig
                    return None
                obj = _checkIntArgs(list(idx), types, types.find('.'))
            except AttributeError:
                return None
        return obj

    def _list_components(self, _obj, cids):
        """
        TODO
        """
        if not cids:
            yield _obj
            return

        name, idx, types = cids[-1]
        try:
            obj = getattr(_obj, name)
        except AttributeError:
            return
        if len(idx) == 0:
            for ans in self._list_components(obj, cids[:-1]):
                yield ans
        elif idx != '**' and '*' not in types and '.' not in types:
            try:
                obj = obj[idx]
            except KeyError:
                return
            for ans in self._list_components(obj, cids[:-1]):
                yield ans
        else:
            all =  idx == '**'
            tList = ComponentUID.tList
            for target_idx, target_obj in iteritems(obj):
                if not all and idx != target_idx:
                    _idx, _types = self._partial_cuid_from_index(target_idx)
                    if len(idx) != len(_idx):
                        continue
                    match = True
                    for j in range(len(idx)):
                        if idx[j] == _idx[j] or types[j] == '*':
                            continue
                        elif types[j] == '.':
                            ok = False
                            for _cast in tList:
                                try:
                                    if _cast(idx[j]) == _idx[j]:
                                        ok = True
                                        break
                                except ValueError:
                                    pass
                            if not ok:
                                match = False
                                break
                        else:
                            match = False
                            break
                    if not match:
                        continue
                for ans in self._list_components(target_obj, cids[:-1]):
                    yield ans

    def list_components(self, block):
        """
        TODO
        """
        for ans in self._list_components(block, self._cids):
            yield ans

    def matches(self, component):
        """
        TODO
        """
        tList = ComponentUID.tList
        for i, (name, idx, types) in enumerate(self._generate_cuid(component)):
            if i == len(self._cids):
                return False
            _n, _idx, _types = self._cids[i]
            if _n != name:
                return False
            if _idx == '**' or idx == _idx:
                continue
            if len(idx) != len(_idx):
                return False
            for j in range(len(idx)):
                if idx[j] == _idx[j] or _types[j] == '*':
                    continue
                elif _types[j] == '.':
                    ok = False
                    for _cast in tList:
                        try:
                            if _cast(_idx[j]) == idx[j]:
                                ok = True
                                break
                        except ValueError:
                            pass
                    if not ok:
                        return False
                else:
                    return False
        # Matched if all self._cids were consumed
        return i+1 == len(self._cids)

# WEH - What does it mean to initialize this dictionary outside
#       of the definition of this class?  Is tList populated
#       with all components???
ComponentUID.tDict.update( (ComponentUID.tKeys[i], v)
                           for i,v in enumerate(ComponentUID.tList) )
ComponentUID.tDict.update( (v, ComponentUID.tKeys[i])
                           for i,v in enumerate(ComponentUID.tList) )
