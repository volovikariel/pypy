import py
from rpython.rtyper.lltypesystem import lltype, llmemory, llarena
from rpython.memory.gc.incminimark import IncrementalMiniMarkGC, WORD
from test_direct import BaseDirectGCTest

S = lltype.GcForwardReference()
S.become(lltype.GcStruct('pinning_test_struct',
                         ('someInt', lltype.Signed),
                         ('next', lltype.Ptr(S))))

class PinningGCTest(BaseDirectGCTest):

    def test_pin_can_move(self):
        # even a pinned object is considered to be movable. Only the caller
        # of pin() knows if it is currently movable or not.
        ptr = self.malloc(S)
        adr = llmemory.cast_ptr_to_adr(ptr)
        assert self.gc.can_move(adr)
        assert self.gc.pin(adr)
        assert self.gc.can_move(adr)

    def test_pin_twice(self):
        ptr = self.malloc(S)
        adr = llmemory.cast_ptr_to_adr(ptr)
        assert self.gc.pin(adr)
        assert not self.gc.pin(adr)

    def test_unpin_not_pinned(self):
        # this test checks a requirement of the unpin() interface
        ptr = self.malloc(S)
        py.test.raises(Exception,
            self.gc.unpin, llmemory.cast_ptr_to_adr(ptr))

    # XXX test with multiple mallocs, and only part of them is pinned


class TestIncminimark(PinningGCTest):
    from rpython.memory.gc.incminimark import IncrementalMiniMarkGC as GCClass

    def test_simple_pin(self):
        ptr = self.malloc(S)
        ptr.someInt = 100
        self.stackroots.append(ptr)

        adr = llmemory.cast_ptr_to_adr(ptr)
        assert self.gc.pin(adr)

        self.gc.collect()
        
        assert self.gc.is_in_nursery(adr)
        assert ptr.someInt == 100

    def test_simple_pin_unpin(self):
        ptr = self.malloc(S)
        ptr.someInt = 100
        self.stackroots.append(ptr)
        adr = llmemory.cast_ptr_to_adr(ptr)
        # check if pin worked
        assert self.gc.pin(adr)
        self.gc.collect()
        assert self.gc.is_in_nursery(adr)
        assert ptr.someInt == 100
        # unpin and check if object is gone from nursery
        self.gc.unpin(adr)
        self.gc.collect()
        py.test.raises(RuntimeError, 'ptr.someInt')
        ptr_old = self.stackroots[0]
        assert ptr_old.someInt == 100

    @py.test.mark.xfail(reason="Not implemented yet", run=False)
    def test_pin_referenced_from_stackroot(self):
        # XXX most likely somehow connected with `old_objects_pointing_to_young`
        # (groggi)
        root_ptr = self.malloc(S)
        next_ptr = self.malloc(S)
        self.write(root_ptr, 'next', next_ptr)
        self.stackroots.append(root_ptr)
        next_ptr.someInt = 100

        next_adr = llmemory.cast_ptr_to_adr(next_ptr)
        assert self.gc.pin(next_adr)

        self.gc.collect()

        assert self.gc.is_in_nursery(adr)
        assert next_ptr.someInt == 100
        root_ptr = self.stackroots[0]
        assert root_ptr.next == next_ptr

    def test_pin_old(self):
        ptr = self.malloc(S)
        ptr.someInt = 100
        self.stackroots.append(ptr)
        self.gc.collect()
        ptr = self.stackroots[0]
        adr = llmemory.cast_ptr_to_adr(ptr)
        assert ptr.someInt == 100
        assert not self.gc.is_in_nursery(adr)
        assert not self.gc.pin(adr)
        # ^^^ should not be possible, struct is already old and won't
        # move.

    def test_pin_malloc_pin(self):
        first_ptr = self.malloc(S)
        first_ptr.someInt = 101
        self.stackroots.append(first_ptr)
        assert self.gc.pin(llmemory.cast_ptr_to_adr(first_ptr))

        self.gc.collect()
        assert first_ptr.someInt == 101

        second_ptr = self.malloc(S)
        second_ptr.someInt = 102
        self.stackroots.append(second_ptr)
        assert self.gc.pin(llmemory.cast_ptr_to_adr(second_ptr))

        self.gc.collect()
        assert first_ptr.someInt == 101
        assert second_ptr.someInt == 102

    def test_pin_shadow_1(self):
        ptr = self.malloc(S)
        adr = llmemory.cast_ptr_to_adr(ptr)
        self.stackroots.append(ptr)
        ptr.someInt = 100
        assert self.gc.pin(adr)
        self.gc.id(ptr) # allocate shadow
        self.gc.minor_collection()
        assert self.gc.is_in_nursery(adr)
        assert ptr.someInt == 100
        self.gc.unpin(adr)
        self.gc.minor_collection() # move to shadow
        adr = llmemory.cast_ptr_to_adr(self.stackroots[0])
        assert not self.gc.is_in_nursery(adr)

    def test_pin_shadow_2(self):
        ptr = self.malloc(S)
        adr = llmemory.cast_ptr_to_adr(ptr)
        self.stackroots.append(ptr)
        ptr.someInt = 100
        assert self.gc.pin(adr)
        self.gc.identityhash(ptr) # allocate shadow
        self.gc.minor_collection()
        assert self.gc.is_in_nursery(adr)
        assert ptr.someInt == 100
        self.gc.unpin(adr)
        self.gc.minor_collection() # move to shadow
        adr = llmemory.cast_ptr_to_adr(self.stackroots[0])
        assert not self.gc.is_in_nursery(adr)

    def test_pin_nursery_top_scenario1(self):
        ptr1 = self.malloc(S)
        adr1 = llmemory.cast_ptr_to_adr(ptr1)
        ptr1.someInt = 101
        self.stackroots.append(ptr1)
        assert self.gc.pin(adr1)
        
        ptr2 = self.malloc(S)
        adr2 = llmemory.cast_ptr_to_adr(ptr2)
        ptr2.someInt = 102
        self.stackroots.append(ptr2)
        assert self.gc.pin(adr2)

        ptr3 = self.malloc(S)
        adr3 = llmemory.cast_ptr_to_adr(ptr3)
        ptr3.someInt = 103
        self.stackroots.append(ptr3)
        assert self.gc.pin(adr3)

        # scenario: no minor collection happened, only three mallocs
        # and pins
        #
        # +- nursery                           nursery_real_top -+
        # |                                                      |
        # v                                                      v
        # +--------+--------+--------+---------------------...---+
        # | pinned | pinned | pinned | empty                     |
        # +--------+--------+--------+---------------------...---+
        #                            ^                           ^
        #                            |                           |
        #              nursery_free -+                           |
        #                                           nursery_top -+
        #
        assert adr3 < self.gc.nursery_free
        assert self.gc.nursery_free < self.gc.nursery_top
        assert self.gc.nursery_top == self.gc.nursery_real_top

    def test_pin_nursery_top_scenario2(self):
        ptr1 = self.malloc(S)
        adr1 = llmemory.cast_ptr_to_adr(ptr1)
        ptr1.someInt = 101
        self.stackroots.append(ptr1)
        assert self.gc.pin(adr1)
        
        ptr2 = self.malloc(S)
        adr2 = llmemory.cast_ptr_to_adr(ptr2)
        ptr2.someInt = 102
        self.stackroots.append(ptr2)
        assert self.gc.pin(adr2)

        ptr3 = self.malloc(S)
        adr3 = llmemory.cast_ptr_to_adr(ptr3)
        ptr3.someInt = 103
        self.stackroots.append(ptr3)
        assert self.gc.pin(adr3)

        # scenario: after first GC minor collection
        #
        # +- nursery                           nursery_real_top -+
        # |                                                      |
        # v                                                      v
        # +--------+--------+--------+---------------------...---+
        # | pinned | pinned | pinned | empty                     |
        # +--------+--------+--------+---------------------...---+
        # ^
        # |
        # +- nursery_free
        # +- nursery_top
        #
        self.gc.minor_collection()

        assert self.gc.nursery_free == self.gc.nursery_top
        assert self.gc.nursery_top == self.gc.nursery
        assert self.gc.nursery_top < adr3
        assert adr3 < self.gc.nursery_real_top

    def test_pin_nursery_top_scenario3(self):
        ptr1 = self.malloc(S)
        adr1 = llmemory.cast_ptr_to_adr(ptr1)
        ptr1.someInt = 101
        self.stackroots.append(ptr1)
        assert self.gc.pin(adr1)
        
        ptr2 = self.malloc(S)
        adr2 = llmemory.cast_ptr_to_adr(ptr2)
        ptr2.someInt = 102
        self.stackroots.append(ptr2)
        assert self.gc.pin(adr2)

        ptr3 = self.malloc(S)
        adr3 = llmemory.cast_ptr_to_adr(ptr3)
        ptr3.someInt = 103
        self.stackroots.append(ptr3)
        assert self.gc.pin(adr3)

        # scenario: after unpinning first object and a minor
        # collection
        #
        # +- nursery                           nursery_real_top -+
        # |                                                      |
        # v                                                      v
        # +--------+--------+--------+---------------------...---+
        # | empty  | pinned | pinned | empty                     |
        # +--------+--------+--------+---------------------...---+
        # ^        ^
        # |        |
        # |        +- nursery_top
        # +- nursery_free
        #
        self.gc.unpin(adr1)
        self.gc.minor_collection()

        assert self.gc.nursery_free == self.gc.nursery
        assert self.gc.nursery_top > self.gc.nursery_free
        assert self.gc.nursery_top < adr2
        assert adr3 < self.gc.nursery_real_top

    def test_pin_nursery_top_scenario4(self):
        ptr1 = self.malloc(S)
        adr1 = llmemory.cast_ptr_to_adr(ptr1)
        ptr1.someInt = 101
        self.stackroots.append(ptr1)
        assert self.gc.pin(adr1)
        
        ptr2 = self.malloc(S)
        adr2 = llmemory.cast_ptr_to_adr(ptr2)
        ptr2.someInt = 102
        self.stackroots.append(ptr2)
        assert self.gc.pin(adr2)

        ptr3 = self.malloc(S)
        adr3 = llmemory.cast_ptr_to_adr(ptr3)
        ptr3.someInt = 103
        self.stackroots.append(ptr3)
        assert self.gc.pin(adr3)

        # scenario: after unpinning first & second object and a minor
        # collection
        #
        # +- nursery                           nursery_real_top -+
        # |                                                      |
        # v                                                      v
        # +-----------------+--------+---------------------...---+
        # | empty           | pinned | empty                     |
        # +-----------------+--------+---------------------...---+
        # ^                 ^
        # |                 |
        # |                 +- nursery_top
        # +- nursery_free
        #
        self.gc.unpin(adr1)
        self.gc.unpin(adr2)
        self.gc.minor_collection()

        assert self.gc.nursery_free == self.gc.nursery
        assert self.gc.nursery_free < self.gc.nursery_top
        assert self.gc.nursery_top < adr3
        assert adr3 < self.gc.nursery_real_top
        
    def test_pin_nursery_top_scenario5(self):
        ptr1 = self.malloc(S)
        adr1 = llmemory.cast_ptr_to_adr(ptr1)
        ptr1.someInt = 101
        self.stackroots.append(ptr1)
        assert self.gc.pin(adr1)
        
        ptr2 = self.malloc(S)
        adr2 = llmemory.cast_ptr_to_adr(ptr2)
        ptr2.someInt = 102
        self.stackroots.append(ptr2)
        assert self.gc.pin(adr2)

        ptr3 = self.malloc(S)
        adr3 = llmemory.cast_ptr_to_adr(ptr3)
        ptr3.someInt = 103
        self.stackroots.append(ptr3)
        assert self.gc.pin(adr3)

        # scenario: no minor collection happened, only three mallocs
        # and pins
        #
        # +- nursery                           nursery_real_top -+
        # |                                                      |
        # v                                                      v
        # +--------+--------+--------+---------------------...---+
        # | pinned | pinned | pinned | empty                     |
        # +--------+--------+--------+---------------------...---+
        #                            ^                           ^
        #                            |                           |
        #              nursery_free -+                           |
        #                                           nursery_top -+
        #
        assert adr3 < self.gc.nursery_free
        assert self.gc.nursery_free < self.gc.nursery_top
        assert self.gc.nursery_top == self.gc.nursery_real_top

        # scenario: unpin everything and minor collection
        #
        # +- nursery                           nursery_real_top -+
        # |                                                      |
        # v                                                      v
        # +----------------------------------+-------------...---+
        # | reset arena                      | empty (not reset) |
        # +----------------------------------+-------------...---+
        # ^                                  ^
        # |                                  |
        # +- nursery_free                    |
        #                       nursery_top -+
        #
        self.gc.unpin(adr1)
        self.gc.unpin(adr2)
        self.gc.unpin(adr3)
        self.gc.minor_collection()

        assert self.gc.nursery_free == self.gc.nursery
        # the following assert is important: make sure that
        # we did not reset the whole arena used as the nursery
        assert self.gc.nursery_top < self.gc.nursery_real_top

    def test_collect_dead_pinned_objects(self):
        # prepare three object, where two are stackroots
        ptr_stackroot_1 = self.malloc(S)
        ptr_stackroot_1.someInt = 100
        self.stackroots.append(ptr_stackroot_1)

        ptr_not_stackroot = self.malloc(S)

        ptr_stackroot_2 = self.malloc(S)
        ptr_stackroot_2.someInt = 100
        self.stackroots.append(ptr_stackroot_2)

        # pin all three objects
        assert self.gc.pin(llmemory.cast_ptr_to_adr(ptr_stackroot_1))
        assert self.gc.pin(llmemory.cast_ptr_to_adr(ptr_not_stackroot))
        assert self.gc.pin(llmemory.cast_ptr_to_adr(ptr_stackroot_2))
        assert self.gc.pinned_objects_in_nursery == 3

        self.gc.minor_collection()
        # now the one not on the stack should be gone.
        assert self.gc.pinned_objects_in_nursery == 2
        assert ptr_stackroot_1.someInt == 100
        assert ptr_stackroot_2.someInt == 100
        py.test.raises(RuntimeError, 'ptr_not_stackroot.someInt') # should be freed

    def test_full_pinned_nursery_pin_fail(self):
        typeid = self.get_type_id(S)
        size = self.gc.fixed_size(typeid) + self.gc.gcheaderbuilder.size_gc_header
        raw_size = llmemory.raw_malloc_usage(size)
        object_mallocs = self.gc.nursery_size // raw_size
        for instance_nr in xrange(object_mallocs):
            ptr = self.malloc(S)
            adr = llmemory.cast_ptr_to_adr(ptr)
            ptr.someInt = 100 + instance_nr
            self.stackroots.append(ptr)
            self.gc.pin(adr)
        # nursery should be full now, at least no space for another `S`. Next malloc should fail.
        py.test.raises(Exception, self.malloc, S)

    def test_pinning_limit(self):
        for instance_nr in xrange(self.gc.max_number_of_pinned_objects):
            ptr = self.malloc(S)
            adr = llmemory.cast_ptr_to_adr(ptr)
            ptr.someInt = 100 + instance_nr
            self.stackroots.append(ptr)
            self.gc.pin(adr)
        #
        # now we reached the maximum amount of pinned objects
        ptr = self.malloc(S)
        adr = llmemory.cast_ptr_to_adr(ptr)
        self.stackroots.append(ptr)
        assert not self.gc.pin(adr)
    test_pinning_limit.GC_PARAMS = {'max_number_of_pinned_objects': 5}

    def test_full_pinned_nursery_pin_fail(self):
        typeid = self.get_type_id(S)
        size = self.gc.fixed_size(typeid) + self.gc.gcheaderbuilder.size_gc_header
        raw_size = llmemory.raw_malloc_usage(size)
        object_mallocs = self.gc.nursery_size // raw_size
        # just to be sure we do not run into the limit as we test not the limiter
        # but rather the case of a nursery full with pinned objects.
        assert object_mallocs < self.gc.max_number_of_pinned_objects
        for instance_nr in xrange(object_mallocs):
            ptr = self.malloc(S)
            adr = llmemory.cast_ptr_to_adr(ptr)
            ptr.someInt = 100 + instance_nr
            self.stackroots.append(ptr)
            self.gc.pin(adr)
        #
        # nursery should be full now, at least no space for another `S`. Next malloc should fail.
        py.test.raises(Exception, self.malloc, S)
    test_full_pinned_nursery_pin_fail.GC_PARAMS = {'max_number_of_pinned_objects': 50}
