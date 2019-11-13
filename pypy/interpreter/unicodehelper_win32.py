from rpython.rtyper.lltypesystem import lltype, rffi
from rpython.rlib.runicode import (BOOLP, WideCharToMultiByte,
         MultiByteToWideChar)
from rpython.rlib.rutf8 import (Utf8StringIterator, next_codepoint_pos,
                                StringBuilder)
from rpython.rlib import rwin32

def Py_UNICODE_HIGH_SURROGATE(ch):
   return rffi.cast(lltype.UniChar, 0xD800 - (0x10000 >> 10) + ((ch) >> 10))

def Py_UNICODE_LOW_SURROGATE(ch):
    return rffi.cast(lltype.UniChar, 0xDC00 + ((ch) & 0x3FF))

if rffi.sizeof(rffi.INT) < rffi.sizeof(rffi.SIZE_T):
    NEED_RETRY = True
else:
    NEED_RETRY = False
WC_ERR_INVALID_CHARS = 0x0080

code_page_map = {
        rwin32.CP_ACP: "mbcs",
        rwin32.CP_UTF7:"CP_UTF7",
        rwin32.CP_UTF8:"CP_UTF8",
    }

def _code_page_name(code_page):
    return code_page_map.get(code_page, "cp%d" % code_page)

def _decode_code_page_flags(code_page):
    if code_page == rwin32.CP_UTF7:
        # The CP_UTF7 decoder only supports flags==0
        return 0
    return rwin32.MB_ERR_INVALID_CHARS

def _encode_code_page_flags(code_page, errors):
    if code_page == rwin32.CP_UTF8:
        return WC_ERR_INVALID_CHARS
    elif code_page == rwin32.CP_UTF7:
        return 0
    if errors == 'replace':
        return 0
    return rwin32.WC_NO_BEST_FIT_CHARS

def _decode_cp_error(s, errorhandler, encoding, errors, start, end):
    # late import to avoid circular import
    from pypy.interpreter.unicodehelper import _str_decode_utf8_slowpath
    if rwin32.GetLastError_saved() == rwin32.ERROR_NO_UNICODE_TRANSLATION:
        msg = ("No mapping for the Unicode character exists in the target "
               "multi-byte code page.")
        r, ignore1, ignore2 = _str_decode_utf8_slowpath(s[start:end], errors, False, errorhandler, False)
        return r, end
    else:
        raise rwin32.lastSavedWindowsError()

def _unibuf_to_utf8(uni, insize):
    """Encode the widechar unicode buffer u to utf8
    Should never error, since the buffer comes from a call to
    MultiByteToWideChar
    """
    flags = 0
    cp = rwin32.CP_UTF8
    used_default_p = lltype.nullptr(BOOLP.TO)
    assert uni is not None
    with rffi.scoped_nonmoving_unicodebuffer(uni) as dataptr:
        # first get the size of the result
        outsize = WideCharToMultiByte(cp, flags, dataptr, insize,
                                    None, 0, None, used_default_p)
        if outsize == 0:
            raise rwin32.lastSavedWindowsError()
        with rffi.scoped_alloc_buffer(outsize) as buf:
            # do the conversion
            if WideCharToMultiByte(cp, flags, dataptr, insize, buf.raw,
                    outsize, None, used_default_p) == 0:
                raise rwin32.lastSavedWindowsError()
            result = buf.str(outsize)
            assert result is not None
            return result

def _decode_helper(cp, s, flags, encoding, errors, errorhandler, 
                   start, end, res):
    if end > len(s):
        end = len(s)
    piece = s[start:end]
    with rffi.scoped_nonmovingbuffer(piece) as dataptr:
        # first get the size of the result
        outsize = MultiByteToWideChar(cp, flags, dataptr, len(piece),
                                    lltype.nullptr(rffi.CWCHARP.TO), 0)
        if outsize == 0:
            r, pos = _decode_cp_error(s, errorhandler,
                                               encoding, errors, start, end)
            res.append(r)
            return pos

        with rffi.scoped_alloc_unicodebuffer(outsize) as buf:
            # do the conversion
            if MultiByteToWideChar(cp, flags, dataptr, len(piece),
                                   buf.raw, outsize) == 0:
                r, pos = _decode_cp_error(s, errorhandler,
                                             encoding, errors, start, end)
                res.append(r)
                return pos
            else:
                res.append(_unibuf_to_utf8(buf.str(outsize), outsize))
    return end

def str_decode_code_page(cp, s, errors, errorhandler, final=False):
    """Decodes a byte string s from a code page cp with an error handler.
    Returns utf8 result, original s length
    """
    insize = len(s)
    if insize == 0:
        return '', 0
    flags = _decode_code_page_flags(cp)
    encoding = _code_page_name(cp)
    assert errorhandler is not None
    res = StringBuilder(insize)
    if errors == 'strict':
        _decode_helper(cp, s, flags, encoding, errors, errorhandler,
                       0, len(s), res)
    else:
        prev_pos = 0
        pos = 0
        while pos < len(s):
            pos = next_codepoint_pos(s, prev_pos)
            pos = _decode_helper(cp, s, flags, encoding,
                            errors, errorhandler, prev_pos, pos, res)
            prev_pos = pos
    return res.build(), insize

def str_decode_mbcs(s, errors, errorhandler, final=False):
    return str_decode_code_page(rwin32.CP_ACP, s, errors, errorhandler, final)

def str_decode_oem(s, errors, errorhandler, final=False):
    return str_decode_code_page(rwin32.CP_OEMCP, s, errors, errorhandler, final)

def utf8_encode_code_page(cp, s, errors, errorhandler):
    """Encode a utf8 string s using code page cp and the given
    errors/errorhandler.
    Returns a encoded byte string
    """

    name = _code_page_name(cp)
    lgt = len(s)

    if lgt == 0:
        return ''
    flags = _encode_code_page_flags(cp, errors)
    if cp in (rwin32.CP_UTF8, rwin32.CP_UTF7):
        used_default_p = lltype.nullptr(BOOLP.TO)
    else:
        used_default_p = lltype.malloc(BOOLP.TO, 1, flavor='raw')
    # Encode one codpoint at a time to allow the errorhandlers to do
    # their thing
    chars = lltype.malloc(rffi.CWCHARP.TO, 2, flavor = 'raw')
    res = StringBuilder(lgt)
    try:
        pos = 0
        for uni in Utf8StringIterator(s):
            if used_default_p:
                used_default_p[0] = rffi.cast(rwin32.BOOL, False)
            if uni < 0x10000:
                chars[0] = rffi.cast(lltype.UniChar, uni)
                charsize = 1
            else:
                chars[0] = Py_UNICODE_HIGH_SURROGATE(uni)
                chars[1] = Py_UNICODE_LOW_SURROGATE(uni)
                charsize = 2
                # first get the size of the result
            outsize = WideCharToMultiByte(cp, flags, chars, charsize, None, 0,
                                           None, used_default_p)
            
            if outsize == 0:
                if rwin32.GetLastError_saved() == rwin32.ERROR_NO_UNICODE_TRANSLATION:
                    r, pos, retype = errorhandler(errors, name,
                                       "invalid character", s, pos, pos+1)
                    res.append(r)
                    continue
                raise rwin32.lastSavedWindowsError()
            # If we used a default char, then we failed!
            if (used_default_p and rffi.cast(lltype.Bool, used_default_p[0])):
                r, pos, retype = errorhandler(errors, name, "invalid character", s, pos, pos+1)
                res.append(r)
                continue
            with rffi.scoped_alloc_buffer(outsize) as buf:
                # do the conversion
                if WideCharToMultiByte(cp, flags,
                                       chars, charsize, buf.raw, outsize,
                                       None, used_default_p) == 0:
                    raise rwin32.lastSavedWindowsError()
                if (used_default_p and
                    rffi.cast(lltype.Bool, used_default_p[0])):
                        r, pos, rettype = errorhandler(errors, name, "invalid character",
                                           s, pos, pos + 1)
                        res.append(r)
                else:
                    result = buf.str(outsize)
                    assert result is not None
                    res.append(result)
            pos += 1
        return res.build()
    finally:
        lltype.free(chars, flavor='raw')
        if used_default_p:
            lltype.free(used_default_p, flavor='raw')

def utf8_encode_mbcs(s, errors, errorhandler):
        return utf8_encode_code_page(rwin32.CP_ACP, s, errors, errorhandler)

def utf8_encode_oem(s, errors, errorhandler):
        return utf8_encode_code_page(rwin32.CP_OEMCP, s, errors, errorhandler)
