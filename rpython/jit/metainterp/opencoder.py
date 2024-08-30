
""" Storage format:
for each operation (inputargs numbered with negative numbers)
<opnum> [size-if-unknown-arity] [<arg0> <arg1> ...] [descr-or-snapshot-index]

Snapshot index for guards points to snapshot stored in _snapshots of trace
"""

from rpython.jit.metainterp.history import (
    ConstInt, Const, ConstFloat, ConstPtr, new_ref_dict, SwitchToBlackhole,
    ConstPtrJitCode)
from rpython.jit.metainterp.resoperation import AbstractResOp, AbstractInputArg,\
    ResOperation, oparity, rop, opwithdescr, GuardResOp, IntOp, FloatOp, RefOp,\
    opclasses
from rpython.rlib.rarithmetic import intmask, r_uint
from rpython.rlib.objectmodel import we_are_translated, specialize, always_inline
from rpython.rlib.jit import Counters
from rpython.rtyper.lltypesystem import rffi, lltype, llmemory
from rpython.rlib.debug import make_sure_not_resized

TAGINT, TAGCONSTPTR, TAGCONSTOTHER, TAGBOX = range(4)
TAGMASK = 0x3
TAGSHIFT = 2

INIT_SIZE = 4096

# right now:
# 2 2 optional n*2 2 optional = at least 4
# 
# idea: do everything in bytes instead of short storage:
# 
# 1 byte opnum,
# 1 byte optional arity,
# varsized args,
# descr-or-snapshot-index (either varsized if known, or leave four bytes for patching)
# 
# XXX todos left:
# - should the snapshots also go somewhere in a more compact form? just right
#   into the byte buffer? or into its own global snapshot buffer?
# - SnapshotIterator is very inefficient

def encode_varint_signed(i, res):
    # https://en.wikipedia.org/wiki/LEB128 signed variant
    more = True
    startlen = len(res)
    while more:
        lowest7bits = i & 0b1111111
        i >>= 7
        if ((i == 0) and (lowest7bits & 0b1000000) == 0) or (
            (i == -1) and (lowest7bits & 0b1000000) != 0
        ):
            more = False
        else:
            lowest7bits |= 0b10000000
        res.append(chr(lowest7bits))
    return len(res) - startlen

@always_inline
def decode_varint_signed(b, index=0):
    res = 0
    shift = 0
    while True:
        byte = ord(b[index])
        res = res | ((byte & 0b1111111) << shift)
        index += 1
        shift += 7
        if not (byte & 0b10000000):
            if byte & 0b1000000:
                res |= -1 << shift
            return res, index


# chosen such that constant ints need at most 4 bytes
SMALL_INT_STOP  = 0x40000
assert encode_varint_signed(SMALL_INT_STOP, []) <= 4
SMALL_INT_START = -0x40001
assert encode_varint_signed(SMALL_INT_START, []) <= 4

class BaseTrace(object):
    pass

class SnapshotIterator(object):
    def __init__(self, main_iter, snapshot):
        self.main_iter = main_iter
        # reverse the snapshots and store the vable, vref lists
        assert isinstance(snapshot, TopSnapshot)
        snapshot_data = main_iter.trace._snapshot_data
        self.vable_array = snapshot.iter_vable_array(snapshot_data)
        self.vref_array = snapshot.iter_vref_array(snapshot_data)
        self.size = self.vable_array.total_length + self.vref_array.total_length + 3
        jc_index, pc = unpack_uint(snapshot.packed_jitcode_pc)
        self.framestack = []
        if jc_index == 2**16-1:
            return
        while snapshot:
            self.framestack.append(snapshot)
            self.size += snapshot.length(snapshot_data) + 2
            snapshot = snapshot.prev
        self.framestack.reverse()

    def iter(self, snapshot):
        return snapshot.iter(self.main_iter.trace._snapshot_data)

    def get(self, index):
        return self.main_iter._untag(index)

    def unpack_jitcode_pc(self, snapshot):
        return unpack_uint(snapshot.packed_jitcode_pc)

    def unpack_array(self, arr):
        # NOT_RPYTHON
        # for tests only
        if isinstance(arr, list):
            arr = BoxArrayIter(arr)
        return [self.get(i) for i in arr]

def _update_liverange(item, index, liveranges, data):
    tag, v = untag(item)
    if tag == TAGBOX:
        liveranges[v] = index

def update_liveranges(snapshot, index, liveranges, data):
    for item in snapshot.iter_vable_array(data):
        _update_liverange(item, index, liveranges, data)
    for item in snapshot.iter_vref_array(data):
        _update_liverange(item, index, liveranges, data)
    while snapshot:
        for item in snapshot.iter(data):
            _update_liverange(item, index, liveranges, data)
        snapshot = snapshot.prev

class TraceIterator(BaseTrace):
    def __init__(self, trace, start, end, force_inputargs=None,
                 metainterp_sd=None):
        self.trace = trace
        self.metainterp_sd = metainterp_sd
        self.all_descr_len = len(metainterp_sd.all_descrs)
        self._cache = [None] * trace._index
        if force_inputargs is not None:
            # the trace here is cut and we're working from
            # inputargs that are in the middle, shuffle stuff around a bit
            self.inputargs = [rop.inputarg_from_tp(arg.type) for
                              arg in force_inputargs]
            for i, arg in enumerate(force_inputargs):
                self._cache[arg.get_position()] = self.inputargs[i]
        else:
            self.inputargs = [rop.inputarg_from_tp(arg.type) for
                              arg in self.trace.inputargs]
            for i, arg in enumerate(self.inputargs):
               self._cache[self.trace.inputargs[i].get_position()] = arg
        self.start = start
        self.pos = start
        self._count = start
        self._index = start
        self.start_index = start
        self.end = end

    def get_dead_ranges(self):
        return self.trace.get_dead_ranges()

    def kill_cache_at(self, pos):
        if pos:
            self._cache[pos] = None

    def _get(self, i):
        res = self._cache[i]
        assert res is not None
        return res

    def done(self):
        return self.pos >= self.end

    def _nextbyte(self):
        if self.done():
            raise IndexError
        res = ord(self.trace._ops[self.pos])
        self.pos += 1
        return res
        
    def _next(self):
        if self.done():
            raise IndexError
        b = self.trace._ops
        index = self.pos
        res = 0
        shift = 0
        while True:
            byte = ord(b[index])
            res = res | ((byte & 0b1111111) << shift)
            index += 1
            shift += 7
            if not (byte & 0b10000000):
                if byte & 0b1000000:
                    res |= -1 << shift
                self.pos = index
                return res

    def _untag(self, tagged):
        tag, v = untag(tagged)
        if tag == TAGBOX:
            return self._get(v)
        elif tag == TAGINT:
            return ConstInt(v + SMALL_INT_START)
        elif tag == TAGCONSTPTR:
            return ConstPtr(self.trace._refs[v])
        elif tag == TAGCONSTOTHER:
            if v & 1:
                return ConstFloat(self.trace._floats[v >> 1])
            else:
                return ConstInt(self.trace._bigints[v >> 1])
        else:
            assert False

    def get_snapshot_iter(self, index):
        return SnapshotIterator(self, self.trace._snapshots[index])

    def next_element_update_live_range(self, index, liveranges):
        opnum = self._nextbyte()
        if oparity[opnum] == -1:
            argnum = self._nextbyte()
        else:
            argnum = oparity[opnum]
        for i in range(argnum):
            tagged = self._next()
            tag, v = untag(tagged)
            if tag == TAGBOX:
                liveranges[v] = index
        if opclasses[opnum].type != 'v':
            liveranges[index] = index
        if opwithdescr[opnum]:
            descr_index = self._next()
            if rop.is_guard(opnum):
                update_liveranges(self.trace._snapshots[descr_index], index,
                                  liveranges, self.trace._snapshot_data)
        if opclasses[opnum].type != 'v':
            return index + 1
        return index

    def next(self):
        opnum = self._nextbyte()
        argnum = oparity[opnum]
        if argnum == -1:
            argnum = self._nextbyte()
        if not (0 <= oparity[opnum] <= 3):
            args = []
            for i in range(argnum):
                args.append(self._untag(self._next()))
            res = ResOperation(opnum, args)
        else:
            cls = opclasses[opnum]
            res = cls()
            argnum = oparity[opnum]
            if argnum == 0:
                pass
            elif argnum == 1:
                res.setarg(0, self._untag(self._next()))
            elif argnum == 2:
                res.setarg(0, self._untag(self._next()))
                res.setarg(1, self._untag(self._next()))
            else:
                assert argnum == 3
                res.setarg(0, self._untag(self._next()))
                res.setarg(1, self._untag(self._next()))
                res.setarg(2, self._untag(self._next()))
        descr_index = -1
        if opwithdescr[opnum]:
            descr_index = self._next()
            if descr_index == 0 or rop.is_guard(opnum):
                descr = None
            else:
                if descr_index < self.all_descr_len + 1:
                    descr = self.metainterp_sd.all_descrs[descr_index - 1]
                else:
                    descr = self.trace._descrs[descr_index - self.all_descr_len - 1]
                res.setdescr(descr)
            if rop.is_guard(opnum): # all guards have descrs
                assert isinstance(res, GuardResOp)
                res.rd_resume_position = descr_index
        if res.type != 'v':
            self._cache[self._index] = res
            self._index += 1
        self._count += 1
        return res

class CutTrace(BaseTrace):
    def __init__(self, trace, start, count, index, inputargs):
        self.trace = trace
        self.start = start
        self.inputargs = inputargs
        self.count = count
        self.index = index

    def cut_at(self, cut):
        assert cut[1] > self.count
        self.trace.cut_at(cut)

    def get_iter(self):
        iter = TraceIterator(self.trace, self.start, self.trace._pos,
                             self.inputargs,
                             metainterp_sd=self.trace.metainterp_sd)
        iter._count = self.count
        iter.start_index = self.index
        iter._index = self.index
        return iter

def combine_uint(index1, index2):
    assert 0 <= index1 < 65536
    assert 0 <= index2 < 65536
    return index1 << 16 | index2 # it's ok to return signed here,
    # we need only 32bit, but 64 is ok for now

def unpack_uint(packed):
    return (packed >> 16) & 0xffff, packed & 0xffff

class BoxArrayIter(object):
    def __init__(self, index, data):
        self.length, self.position = decode_varint_signed(data, index)
        self.total_length = self.length
        self.data = data

    def __iter__(self):
        return self

    def next(self):
        if self.length == 0:
            raise StopIteration
        self.length -= 1
        item, self.position = decode_varint_signed(self.data, self.position)
        return item


class Snapshot(object):
    """ snapshot array data is stored in Trace._snapshot_data.
    The format of every array is:
    length, box1, ..., boxn
    
    The start of the data is given by box_array_index.
    """

    _attrs_ = ('packed_jitcode_pc', 'box_array_index', 'prev')

    prev = None

    def __init__(self, packed_jitcode_pc, box_array_index):
        self.packed_jitcode_pc = packed_jitcode_pc
        self.box_array_index = box_array_index

    def length(self, data):
        return decode_varint_signed(data, self.box_array_index)[0]

    def iter(self, data):
        return BoxArrayIter(self.box_array_index, data)


class TopSnapshot(Snapshot):
    def __init__(self, packed_jitcode_pc, box_array_index, vable_array, vref_array):
        Snapshot.__init__(self, packed_jitcode_pc, box_array_index)
        self.vable_array_index = vable_array
        self.vref_array_index = vref_array

    def iter_vable_array(self, data):
        return BoxArrayIter(self.vable_array_index, data)

    def iter_vref_array(self, data):
        return BoxArrayIter(self.vref_array_index, data)


class Trace(BaseTrace):
    _deadranges = (-1, None)

    def __init__(self, max_num_inputargs, metainterp_sd):
        self.metainterp_sd = metainterp_sd
        self._ops = ['\x00'] * INIT_SIZE
        make_sure_not_resized(self._ops)
        self._pos = 0
        self._consts_bigint = 0
        self._consts_float = 0
        self._total_snapshots = 0
        self._consts_ptr = 0
        self._consts_ptr_nodict = 0
        self._descrs = [None]
        self._refs = [lltype.nullptr(llmemory.GCREF.TO)]
        self._refs_dict = new_ref_dict()
        self._bigints = []
        self._bigints_dict = {}
        self._floats = []
        self._snapshots = []
        self._snapshot_data = ['\x00']
        if not we_are_translated() and isinstance(max_num_inputargs, list): # old api for tests
            self.inputargs = max_num_inputargs
            for i, box in enumerate(max_num_inputargs):
                box.position_and_flags = r_uint(i << 1)
            max_num_inputargs = len(max_num_inputargs)

        self.max_num_inputargs = max_num_inputargs
        self._count = max_num_inputargs # total count
        self._index = max_num_inputargs # "position" of resulting resops
        self._start = max_num_inputargs
        self._pos = max_num_inputargs
        self.tag_overflow = False

    def set_inputargs(self, inputargs):
        self.inputargs = inputargs
        if not we_are_translated():
            set_positions = {box.get_position() for box in inputargs}
            assert len(set_positions) == len(inputargs)
            assert not set_positions or max(set_positions) < self.max_num_inputargs

    def _double_ops(self):
        self._ops = self._ops + ['\x00'] * len(self._ops)

    def append_byte(self, c):
        assert 0 <= c < 256
        if self._pos >= len(self._ops):
            self._double_ops()
        self._ops[self._pos] = chr(c)
        self._pos += 1

    def append_int(self, i):
        more = True
        startlen = self._pos
        while more:
            lowest7bits = i & 0b1111111
            i >>= 7
            if ((i == 0) and (lowest7bits & 0b1000000) == 0) or (
                (i == -1) and (lowest7bits & 0b1000000) != 0
            ):
                more = False
            else:
                lowest7bits |= 0b10000000
            self.append_byte(lowest7bits)

    def tracing_done(self):
        from rpython.rlib.debug import debug_start, debug_stop, debug_print
        self._bigints_dict = {}
        self._refs_dict = new_ref_dict()
        debug_start("jit-trace-done")
        debug_print("trace length:", self._pos)
        debug_print(" total snapshots:", self._total_snapshots)
        debug_print(" bigint consts: " + str(self._consts_bigint), len(self._bigints))
        debug_print(" float consts: " + str(self._consts_float), len(self._floats))
        debug_print(" ref consts: " + str(self._consts_ptr) + " " + str(self._consts_ptr_nodict),  len(self._refs))
        debug_print(" descrs:", len(self._descrs))
        debug_stop("jit-trace-done")

    def length(self):
        return self._pos

    def cut_point(self):
        return self._pos, self._count, self._index

    def cut_at(self, end):
        self._pos = end[0]
        self._count = end[1]
        self._index = end[2]

    def cut_trace_from(self, (start, count, index), inputargs):
        return CutTrace(self, start, count, index, inputargs)

    def _cached_const_int(self, box):
        return v

    def _cached_const_ptr(self, box):
        assert isinstance(box, ConstPtr)
        addr = box.getref_base()
        if not addr:
            return 0
        if isinstance(box, ConstPtrJitCode):
            index = box.opencoder_index
            if index >= 0:
                self._consts_ptr_nodict += 1
                assert self._refs[index] == addr
                return index
        v = self._refs_dict.get(addr, -1)
        if v == -1:
            v = len(self._refs)
            self._refs_dict[addr] = v
            self._refs.append(addr)
        if isinstance(box, ConstPtrJitCode):
            box.opencoder_index = v
        return v

    def _encode(self, box):
        if isinstance(box, Const):
            if (isinstance(box, ConstInt) and
                isinstance(box.getint(), int) and # symbolics
                SMALL_INT_START <= box.getint() < SMALL_INT_STOP):
                return tag(TAGINT, box.getint() - SMALL_INT_START)
            elif isinstance(box, ConstInt):
                self._consts_bigint += 1
                value = box.getint()
                if not isinstance(value, int):
                    # symbolics, for tests, don't worry about caching
                    v = len(self._bigints) << 1
                    self._bigints.append(value)
                else:
                    v = self._bigints_dict.get(value, -1)
                    if v == -1:
                        v = len(self._bigints) << 1
                        self._bigints_dict[value] = v
                        self._bigints.append(value)
                return tag(TAGCONSTOTHER, v)
            elif isinstance(box, ConstFloat):
                # don't intern float constants
                self._consts_float += 1
                v = (len(self._floats) << 1) | 1
                self._floats.append(box.getfloatstorage())
                return tag(TAGCONSTOTHER, v)
            else:
                self._consts_ptr += 1
                v = self._cached_const_ptr(box)
                return tag(TAGCONSTPTR, v)
        elif isinstance(box, AbstractResOp):
            assert box.get_position() >= 0
            return tag(TAGBOX, box.get_position())
        else:
            assert False, "unreachable code"

    def _op_start(self, opnum, num_argboxes):
        old_pos = self._pos
        self.append_byte(opnum)
        expected_arity = oparity[opnum]
        if expected_arity == -1:
            self.append_byte(num_argboxes)
        else:
            assert num_argboxes == expected_arity
        return old_pos

    def _op_end(self, opnum, descr, old_pos):
        if opwithdescr[opnum]:
            if descr is None:
                self.append_byte(0)
            else:
                self.append_int(self._encode_descr(descr))
        self._count += 1
        if opclasses[opnum].type != 'v':
            self._index += 1

    def record_op(self, opnum, argboxes, descr=None):
        pos = self._index
        old_pos = self._op_start(opnum, len(argboxes))
        for box in argboxes:
            self.append_int(self._encode(box))
        self._op_end(opnum, descr, old_pos)
        return pos

    def record_op0(self, opnum, descr=None):
        pos = self._index
        old_pos = self._op_start(opnum, 0)
        self._op_end(opnum, descr, old_pos)
        return pos

    def record_op1(self, opnum, argbox1, descr=None):
        pos = self._index
        old_pos = self._op_start(opnum, 1)
        self.append_int(self._encode(argbox1))
        self._op_end(opnum, descr, old_pos)
        return pos

    def record_op2(self, opnum, argbox1, argbox2, descr=None):
        pos = self._index
        old_pos = self._op_start(opnum, 2)
        self.append_int(self._encode(argbox1))
        self.append_int(self._encode(argbox2))
        self._op_end(opnum, descr, old_pos)
        return pos

    def record_op3(self, opnum, argbox1, argbox2, argbox3, descr=None):
        pos = self._index
        old_pos = self._op_start(opnum, 3)
        self.append_int(self._encode(argbox1))
        self.append_int(self._encode(argbox2))
        self.append_int(self._encode(argbox3))
        self._op_end(opnum, descr, old_pos)
        return pos

    def _encode_descr(self, descr):
        descr_index = descr.get_descr_index()
        if descr_index != -1:
            return descr_index + 1
        self._descrs.append(descr)
        return len(self._descrs) - 1 + len(self.metainterp_sd.all_descrs) + 1

    def _list_of_boxes(self, boxes):
        boxes_list_storage = self.new_array(len(boxes))
        for i in range(len(boxes)):
            boxes_list_storage = self._add_box_to_storage(boxes_list_storage, boxes[i])
        return boxes_list_storage

    def new_array(self, lgt):
        if lgt == 0:
            return 0
        res = len(self._snapshot_data)
        self.append_snapshot_int(lgt)
        return res

    def _add_box_to_storage(self, boxes_list_storage, box):
        self.append_snapshot_int(self._encode(box))
        return boxes_list_storage

    def append_snapshot_int(self, i):
        encode_varint_signed(i, self._snapshot_data)

    def create_top_snapshot(self, jitcode, pc, frame, vable_boxes, vref_boxes, after_residual_call=False):
        self._total_snapshots += 1
        array = frame.get_list_of_active_boxes(False, self.new_array, self._add_box_to_storage,
                after_residual_call=after_residual_call)
        vable_array = self._list_of_boxes(vable_boxes)
        vref_array = self._list_of_boxes(vref_boxes)
        s = TopSnapshot(combine_uint(jitcode.index, pc), array, vable_array,
                        vref_array)
        # guards have no descr
        self._snapshots.append(s)
        assert self._ops[self._pos - 1] == '\x00'
        self._pos -= 1
        self.append_int(len(self._snapshots) - 1)
        return s

    def create_empty_top_snapshot(self, vable_boxes, vref_boxes):
        self._total_snapshots += 1
        array = self._list_of_boxes([])
        vable_array = self._list_of_boxes(vable_boxes)
        vref_array = self._list_of_boxes(vref_boxes)
        s = TopSnapshot(combine_uint(2**16 - 1, 0), array, vable_array,
                        vref_array)
        # guards have no descr
        self._snapshots.append(s)
        if not self.tag_overflow: # otherwise we're broken anyway
            assert self._ops[self._pos - 1] == '\x00'
            self._pos -= 1
            self.append_int(len(self._snapshots) - 1)
        return s

    def create_snapshot(self, jitcode, pc, frame, flag):
        self._total_snapshots += 1
        array = frame.get_list_of_active_boxes(flag, self.new_array, self._add_box_to_storage)
        return Snapshot(combine_uint(jitcode.index, pc), array)

    def get_iter(self):
        return TraceIterator(self, self._start, self._pos,
                             metainterp_sd=self.metainterp_sd)

    def get_live_ranges(self):
        t = self.get_iter()
        liveranges = [0] * self._index
        index = t._count
        while not t.done():
            index = t.next_element_update_live_range(index, liveranges)
        return liveranges

    def get_dead_ranges(self):
        """ Same as get_live_ranges, but returns a list of "dying" indexes,
        such as for each index x, the number found there is for sure dead
        before x
        """
        def insert(ranges, pos, v):
            # XXX skiplist
            while ranges[pos]:
                pos += 1
                if pos == len(ranges):
                    return
            ranges[pos] = v

        if self._deadranges != (-1, None):
            if self._deadranges[0] == self._count:
                return self._deadranges[1]
        liveranges = self.get_live_ranges()
        deadranges = [0] * (self._index + 2)
        assert len(deadranges) == len(liveranges) + 2
        for i in range(self._start, len(liveranges)):
            elem = liveranges[i]
            if elem:
                insert(deadranges, elem + 1, i)
        self._deadranges = (self._count, deadranges)
        return deadranges

    def unpack(self):
        iter = self.get_iter()
        ops = []
        try:
            while True:
                ops.append(iter.next())
        except IndexError:
            pass
        return iter.inputargs, ops

def tag(kind, pos):
    res = intmask(r_uint(pos) << TAGSHIFT)
    assert res >> TAGSHIFT == pos
    return (pos << TAGSHIFT) | kind

@specialize.ll()
def untag(tagged):
    return intmask(tagged) & TAGMASK, intmask(tagged) >> TAGSHIFT
