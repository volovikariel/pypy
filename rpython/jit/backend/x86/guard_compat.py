from rpython.rlib import rgc
from rpython.rlib.objectmodel import we_are_translated
from rpython.rtyper.lltypesystem import lltype, rffi
from rpython.jit.backend.x86.arch import WORD, IS_X86_32, IS_X86_64
from rpython.jit.backend.x86 import rx86, codebuf
from rpython.jit.backend.x86.regloc import X86_64_SCRATCH_REG, imm, eax, edx
from rpython.jit.backend.llsupport.asmmemmgr import MachineDataBlockWrapper
from rpython.jit.metainterp.compile import GuardCompatibleDescr
from rpython.jit.metainterp.history import BasicFailDescr


# uses the raw structure COMPATINFO, which is informally defined like this:
# it starts with a negative 'small_ofs' value (see in the code)
# then there is an array containing all the expected values that should pass
# the guard, ending in -1.


# --tweakable parameters (you get the effect closest to before we had
# guard-compat by setting GROW_POSITION to 1 and UPDATE_ASM to 0)--

# where grow_switch puts the new value:
#   0 = at the beginning of the list
#   1 = at position N-1, just before the initial value which stays last
#   2 = at the end
GROW_POSITION = 2

# when guard_compatible's slow path is called and finds a value, when
# should we update the machine code to make this value the fast-path?
#   0 = never
#   another value = after about this many calls to the slow-path
UPDATE_ASM = 1291


def generate_guard_compatible(assembler, guard_token, loc_reg, initial_value):
    # fast-path check
    mc = assembler.mc
    if IS_X86_64:
        mc.MOV_ri64(X86_64_SCRATCH_REG.value, initial_value)
        rel_pos_compatible_imm = mc.get_relative_pos()
        mc.CMP_rr(loc_reg.value, X86_64_SCRATCH_REG.value)
    elif IS_X86_32:
        mc.CMP_ri32(loc_reg.value, initial_value)
        rel_pos_compatible_imm = mc.get_relative_pos()
    mc.J_il8(rx86.Conditions['E'], 0)
    je_location = mc.get_relative_pos()

    # fast-path failed, call the slow-path checker
    checker = get_or_build_checker(assembler, loc_reg.value)

    # initialize 'compatinfo' with only 'initial_value' in it
    compatinfoaddr = assembler.datablockwrapper.malloc_aligned(
        3 * WORD, alignment=WORD)
    compatinfo = rffi.cast(rffi.SIGNEDP, compatinfoaddr)
    compatinfo[1] = initial_value
    compatinfo[2] = -1

    if IS_X86_64:
        mc.MOV_ri64(X86_64_SCRATCH_REG.value, compatinfoaddr)  # patchable
        guard_token.pos_compatinfo_offset = mc.get_relative_pos() - WORD
        mc.PUSH_r(X86_64_SCRATCH_REG.value)
    elif IS_X86_32:
        mc.PUSH_i32(compatinfoaddr)   # patchable
        guard_token.pos_compatinfo_offset = mc.get_relative_pos() - WORD
    mc.CALL(imm(checker))
    mc.stack_frame_size_delta(-WORD)

    small_ofs = rel_pos_compatible_imm - mc.get_relative_pos()
    assert -128 <= small_ofs < 128
    compatinfo[0] = small_ofs & 0xFF

    assembler.guard_success_cc = rx86.Conditions['Z']
    assembler.implement_guard(guard_token)
    #
    # patch the JE above
    offset = mc.get_relative_pos() - je_location
    assert 0 < offset <= 127
    mc.overwrite(je_location-1, chr(offset))


def patch_guard_compatible(rawstart, tok):
    descr = tok.faildescr
    if not we_are_translated() and isinstance(descr, BasicFailDescr):
        pass    # for tests
    else:
        assert isinstance(descr, GuardCompatibleDescr)
    descr._backend_compatinfo = rawstart + tok.pos_compatinfo_offset


def grow_switch(cpu, compiled_loop_token, guarddescr, gcref):
    # XXX is it ok to force gcref to be non-movable?
    if not rgc._make_sure_does_not_move(gcref):
        raise AssertionError("oops")
    new_value = rffi.cast(lltype.Signed, gcref)

    # XXX related to the above: for now we keep alive the gcrefs forever
    # in the compiled_loop_token
    if compiled_loop_token._keepalive_extra is None:
        compiled_loop_token._keepalive_extra = []
    compiled_loop_token._keepalive_extra.append(gcref)

    if not we_are_translated() and isinstance(guarddescr, BasicFailDescr):
        pass    # for tests
    else:
        assert isinstance(guarddescr, GuardCompatibleDescr)
    compatinfop = rffi.cast(rffi.VOIDPP, guarddescr._backend_compatinfo)
    compatinfo = rffi.cast(rffi.SIGNEDP, compatinfop[0])
    length = 3
    while compatinfo[length - 1] != -1:
        length += 1

    allblocks = compiled_loop_token.get_asmmemmgr_blocks()
    datablockwrapper = MachineDataBlockWrapper(cpu.asmmemmgr, allblocks)
    newcompatinfoaddr = datablockwrapper.malloc_aligned(
        (length + 1) * WORD, alignment=WORD)
    datablockwrapper.done()

    newcompatinfo = rffi.cast(rffi.SIGNEDP, newcompatinfoaddr)
    newcompatinfo[0] = compatinfo[0]

    if GROW_POSITION == 0:
        newcompatinfo[1] = new_value
        for i in range(1, length):
            newcompatinfo[i + 1] = compatinfo[i]
    elif GROW_POSITION == 1:
        for i in range(1, length - 2):
            newcompatinfo[i] = compatinfo[i]
        newcompatinfo[length - 2] = new_value
        newcompatinfo[length - 1] = compatinfo[length - 2]
        newcompatinfo[length] = -1    # == compatinfo[length - 1]
    else:
        for i in range(1, length - 1):
            newcompatinfo[i] = compatinfo[i]
        newcompatinfo[length - 1] = new_value
        newcompatinfo[length] = -1    # == compatinfo[length - 1]

    # the old 'compatinfo' is not used any more, but will only be freed
    # when the looptoken is freed
    compatinfop[0] = rffi.cast(rffi.VOIDP, newcompatinfo)

    # the machine code is not updated here.  We leave it to the actual
    # guard_compatible to update it if needed.


def setup_once(assembler):
    nb_registers = WORD * 2
    assembler._guard_compat_checkers = [0] * nb_registers


def _build_inner_loop(mc, regnum, tmp, immediate_return):
    pos = mc.get_relative_pos()
    mc.CMP_mr((tmp, WORD), regnum)
    mc.J_il8(rx86.Conditions['E'], 0)    # patched below
    je_location = mc.get_relative_pos()
    mc.CMP_mi((tmp, WORD), -1)
    mc.LEA_rm(tmp, (tmp, WORD))
    mc.J_il8(rx86.Conditions['NE'], pos - (mc.get_relative_pos() + 2))
    #
    # not found!  Return the condition code 'Not Zero' to mean 'not found'.
    mc.OR_rr(tmp, tmp)
    #
    # if 'immediate_return', patch the JE above to jump here.  When we
    # follow that path, we get condition code 'Zero', which means 'found'.
    if immediate_return:
        offset = mc.get_relative_pos() - je_location
        assert 0 < offset <= 127
        mc.overwrite(je_location-1, chr(offset))
    #
    if IS_X86_32:
        mc.POP_r(tmp)
    mc.RET16_i(WORD)
    mc.force_frame_size(8)   # one word on X86_64, two words on X86_32
    #
    # if not 'immediate_return', patch the JE above to jump here.
    if not immediate_return:
        offset = mc.get_relative_pos() - je_location
        assert 0 < offset <= 127
        mc.overwrite(je_location-1, chr(offset))

def get_or_build_checker(assembler, regnum):
    """Returns a piece of assembler that checks if the value is in
    some array (there is one such piece per input register 'regnum')
    """
    addr = assembler._guard_compat_checkers[regnum]
    if addr != 0:
        return addr

    mc = codebuf.MachineCodeBlockWrapper()

    if IS_X86_64:
        tmp = X86_64_SCRATCH_REG.value
        stack_ret = 0
        stack_arg = WORD
    elif IS_X86_32:
        if regnum != eax.value:
            tmp = eax.value
        else:
            tmp = edx.value
        mc.PUSH_r(tmp)
        stack_ret = WORD
        stack_arg = 2 * WORD

    mc.MOV_rs(tmp, stack_arg)

    if UPDATE_ASM > 0:
        CONST_TO_ADD = int((1 << 24) / (UPDATE_ASM + 0.3))
        if CONST_TO_ADD >= (1 << 23):
            CONST_TO_ADD = (1 << 23) - 1
        if CONST_TO_ADD < 1:
            CONST_TO_ADD = 1
        CONST_TO_ADD <<= 8
        #
        mc.ADD32_mi32((tmp, 0), CONST_TO_ADD)
        mc.J_il8(rx86.Conditions['C'], 0)    # patched below
        jc_location = mc.get_relative_pos()
    else:
        jc_location = -1

    _build_inner_loop(mc, regnum, tmp, immediate_return=True)

    if jc_location != -1:
        # patch the JC above
        offset = mc.get_relative_pos() - jc_location
        assert 0 < offset <= 127
        mc.overwrite(jc_location-1, chr(offset))
        #
        _build_inner_loop(mc, regnum, tmp, immediate_return=False)
        #
        # found!  update the assembler by writing the value at 'small_ofs'
        # bytes before our return address.  This should overwrite the const in
        # 'MOV_ri64(r11, const)', first instruction of the guard_compatible.
        mc.MOV_rs(tmp, stack_arg)
        mc.MOVSX8_rm(tmp, (tmp, 0))
        mc.ADD_rs(tmp, stack_ret)
        mc.MOV_mr((tmp, -WORD), regnum)
        #
        # Return condition code 'Zero' to mean 'found'.
        mc.CMP_rr(regnum, regnum)
        if IS_X86_32:
            mc.POP_r(tmp)
        mc.RET16_i(WORD)

    addr = mc.materialize(assembler.cpu, [])
    assembler._guard_compat_checkers[regnum] = addr
    return addr
