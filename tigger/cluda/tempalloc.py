import collections

from tigger.helpers import AttrDict
from tigger.helpers.sortedcollection import SortedCollection


class ValueHolder:
    """
    This class serves as a connection between the memory manager and the allocation.
    We need both the manager to keep the reference to the allocation
    (to change it after memory pack), and the allocation to keep the reference to the manager
    (to notify it when the allocation is freed).
    The resulting reference loop is avoided by using this "third-party" object.
    """

    def __init__(self):
        self.value = None

    def __repr__(self):
        return "ValueHolder(" + repr(self.value) + ")"


class DynamicAllocation:
    """
    This class is a wrapper for the allocation.
    As far as :py:class:`~tigger.cluda.api.Array` class is concerned,
    it behaves the same as :py:class:`pycuda.driver.DeviceAllocation` or :py:class:`pyopencl.Buffer`.
    """

    def __init__(self, manager, id, value_holder):
        self._id = id
        self._value_holder = value_holder
        self._manager = manager

    def __int__(self):
        return int(self._value_holder.value)

    def __long__(self):
        return long(self._value_holder.value)

    def __del__(self):
        # notify the memory manager that the allocation is no longer needed.
        self._manager.free(self._id)


def extract_dependencies(dependencies):
    """
    Recursively extracts allocation identifiers from an iterable or Array class.
    """
    results = set()

    if isinstance(dependencies, collections.Iterable):
        for dep in dependencies:
            results.update(extract_dependencies(dep))
    elif hasattr(dependencies, '__tempalloc__'):
        # hook for exposing temporary allocations in arbitrary classes
        results.update(extract_dependencies(dependencies.__tempalloc__))
    elif isinstance(dependencies, DynamicAllocation):
        return set([dependencies._id])
    elif hasattr(dependencies, 'data'):
        # checking for PyOpenCL Array
        results.update(extract_dependencies(dependencies.data))
    elif hasattr(dependencies, 'gpudata'):
        # checking for PyCUDA GPUArray
        results.update(extract_dependencies(dependencies.gpudata))

    return results


class TemporaryAllocator:
    """
    This class has the standard ``allocator`` interface for the
    :py:class:`~tigger.cluda.api.Array` constructor.

    :param manager: instance of a class derived from
        :py:class:`~tigger.cluda.tempalloc.TemporaryManager`.
    :param dependencies: can be a :py:class:`~tigger.cluda.api.Array` instance
        (the ones containing persistent allocations will be ignored),
        a :py:class:`~tigger.cluda.tempalloc.DynamicAllocation` instance,
        an iterable with valid values,
        or an object with the attribute `__tempalloc__` which is a valid value
        (the last two will be processed recursively).
    """

    def __init__(self, manager, dependencies=None):
        assert isinstance(manager, TemporaryManager)
        self._manager = manager

        # extracting dependencies right away in case given list
        # is changed between __init__ and __call__
        self._dependencies = extract_dependencies(dependencies)

    def __call__(self, size):
        return self._manager.allocate(size, self._dependencies)


class TemporaryManager:
    """
    Base class for a manager of temporary allocations.

    :param ctx: an instance of :py:class:`~tigger.cluda.api.Context`.
    :param pack_on_alloc: whether to repack allocations when a new allocation is requested.
    :param pack_on_free: whether to repack allocations when an allocation is freed.
    """

    def __init__(self, ctx, pack_on_alloc=False, pack_on_free=False):
        self._ctx = ctx
        self._id_counter = 0
        self._pack_on_alloc = pack_on_alloc
        self._pack_on_free = pack_on_free

    def allocate(self, size, dependencies=None):
        """
        Processes an allocation request for ``size`` bytes with an iterable of dependencies' ids.
        Returns an instance of :py:class:`~tigger.cluda.tempalloc.DynamicAllocation`.
        """

        new_id = self._id_counter
        self._id_counter += 1

        # dependencies come already processed (a set of ids) to this method
        vh = self._allocate(new_id, size,
            dependencies, self._pack_on_alloc)

        return DynamicAllocation(self, new_id, vh)

    def free(self, id):
        """
        Frees the allocation with given ``id``.
        """
        self._free(id, self._pack_on_free)

    def pack(self):
        """
        Packs the real allocations possibly reducing total memory usage.
        This process can be slow.
        """
        self._pack()


class TrivialManager(TemporaryManager):
    """
    Trivial manager --- allocates a separate buffer for each allocation request.
    """

    def __init__(self, *args, **kwds):
        TemporaryManager.__init__(self, *args, **kwds)
        self._allocations = {}

    def _allocate(self, new_id, size, dependencies, pack):
        vh = ValueHolder(self._ctx.allocate(size))
        self._allocations[new_id] = vh
        return vh

    def _free(self, id, pack):
        del self._allocations[id]

    def _pack(self):
        pass


class ZeroOffsetManager(TemporaryManager):
    """
    Tries to assign several allocation requests to a single real allocation,
    if dependencies allow that.
    All virtual allocations start from the beginning of real allocations.
    """

    def __init__(self, *args, **kwds):
        TemporaryManager.__init__(self, *args, **kwds)

        self._virtual_allocations = {} # id -> (size, set(dependencies))
        self._real_sizes = SortedCollection(key=lambda x: x.size) # (size, real_id), sorted by size
        self._virtual_to_real = {} # id -> (real_id, ValueHolder)
        self._real_allocations = {} # real_id -> (buffer, set(id))
        self._real_id_counter = 0

    def _allocate(self, new_id, size, dependencies, pack):

        # Dependencies should be bidirectional.
        # So if some new allocation says it depends on earlier ones,
        # we need to update their dependency lists.
        dep_set = set(dependencies)
        for dep in dependencies:
            if dep in self._virtual_allocations:
                self._virtual_allocations[dep].dependencies.add(new_id)
            else:
                dep_set.remove(dep)

        # Save virtual allocation parameters
        self._virtual_allocations[new_id] = AttrDict(size=size, dependencies=dep_set)
        vh = ValueHolder()
        self._virtual_to_real[new_id] = AttrDict(real_id=None, value_holder=vh)

        if pack:
            # If pack is requested, we can just do full re-pack right away.
            self._pack()
        else:
            # If not, find a real allocation using the greedy algorithm.
            self._fast_add(new_id, size, dep_set)

        return vh

    def _fast_add(self, new_id, size, dep_set):
        """
        Greedy algorithm to find a real allocation for a given virtual allocation.
        """

        # Find the smallest real allocation which can hold the requested virtual allocation.
        try:
            idx_start = self._real_sizes.argfind_ge(size)
        except ValueError:
            idx_start = 0

        # Check all real allocations with suitable sizes, starting from the smallest one.
        # Use the first real allocation which does not contain ``new_id``'s dependencies.
        for idx in range(idx_start, len(self._real_sizes)):
            real_id = self._real_sizes[idx].real_id
            buf = self._real_allocations[real_id].buffer
            virtual_ids = self._real_allocations[real_id].virtual_ids
            if virtual_ids.isdisjoint(dep_set):
                virtual_ids.add(new_id)
                break
        else:
            # If no suitable real allocation is found, create a new one.
            buf = self._ctx.allocate(size)
            real_id = self._real_id_counter
            self._real_id_counter += 1

            self._real_allocations[real_id] = AttrDict(buffer=buf, virtual_ids=set([new_id]))
            self._real_sizes.insert(AttrDict(size=size, real_id=real_id))

        self._virtual_to_real[new_id].real_id = real_id
        self._virtual_to_real[new_id].value_holder.value = buf

    def _free(self, id, pack=False):

        # Remove the allocation from the dependency lists of its dependencies
        dep_set = self._virtual_allocations[id].dependencies
        for dep in dep_set:
            self._virtual_allocations[dep].dependencies.remove(id)

        # Clear value holder (to make possible after-deallocation accesses easier to notice)
        vtr = self._virtual_to_real[id]
        vtr.value_holder.value = None

        # Clear virtual allocation data
        del self._virtual_allocations[id]
        del self._virtual_to_real[id]

        if pack:
            self._pack()
        else:
            # Fast and non-optimal free.
            # Remove the virtual allocation from the real allocation,
            # and delete the real allocation if its no longer used by other virtual allocations.
            ra = self._real_allocations[vtr.real_id]
            ra.virtual_ids.remove(id)
            if len(ra.virtual_ids) == 0:
                del self._real_allocations[vtr.real_id]
                self._real_sizes.remove(AttrDict(size=ra.buffer.size, real_id=vtr.real_id))

    def _pack(self):
        """
        Full memory re-pack.
        In theory, should find the optimal (with the minimal real allocation size) distribution
        of virtual allocations.
        """

        # Need to synchronize, because we are going to change allocation addresses,
        # and we do not want to free the memory some kernel is reading from.
        self._ctx.synchronize()

        # Clear all real allocation data.
        self._real_sizes.clear()
        self._real_allocations = {}
        self._real_id_counter = 0

        va = self._virtual_allocations

        # Sort all virtual allocations by size
        virtual_sizes = sorted([AttrDict(size=va[id].size, id=id)
            for id in va], key=lambda x: x.size)

        # Application of greedy algorithm for virtual allocations starting from the largest one
        # should give the optimal distribution.
        for size, id in reversed(virtual_sizes):
            self._fast_add(id, size, self._virtual_allocations[id].dependencies)

    def _statistics(self):

        stats = AttrDict(
            virtual_size_total=0,
            virtual_num=0,
            real_size_total=0,
            real_num=0,
            virtual_sizes=[],
            real_sizes=[])

        for id, va in self._virtual_allocations.items():
            stats.virtual_size_total += va.size
            stats.virtual_num += 1
            stats.virtual_sizes.append(va.size)

        for id, ra in self._real_allocations.items():
            stats.real_size_total += ra.buffer.size
            stats.real_num += 1
            stats.real_sizes.append(ra.buffer.size)

        stats.virtual_sizes = sorted(stats.virtual_sizes)
        stats.real_sizes = sorted(stats.real_sizes)

        return stats


if __name__ == '__main__':

    import numpy
    import tigger.cluda as cluda
    api = cluda.ocl_api()
    ctx = api.Context.create()

    manager = ZeroOffsetManager(ctx)

    arr = ctx.temp_array(1000, numpy.int32)
    print ctx.temp_alloc._statistics()
    arr2 = ctx.temp_array(1000, numpy.int32, dependencies=arr)
    arr3 = ctx.temp_array(1000, numpy.int32)
    print ctx.temp_alloc._statistics()

    del arr
    del arr2

    print ctx.temp_alloc._statistics()