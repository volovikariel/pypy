from pytest import raises

def test_simple():
    try:
        raise TypeError()
    except* TypeError:
        a = 1
    except* ValueError:
        a = 2
    assert a == 1

def test_both_excepts_run():
    l = []
    try:
        raise ExceptionGroup('abc', [ValueError(), TypeError()])
    except* TypeError:
        l.append(1)
    except* ValueError:
        l.append(2)
    print(l)
    assert l == [1, 2]

def raises_one():
    try:
        raise ExceptionGroup('abc', [ValueError(), TypeError()])
    except* TypeError:
        pass

def test_reraise():
    a = 1
    try:
        raises_one()
    except* ValueError:
        a = 0
    assert a == 0 # and in particular, we reach this line

def error_in_handler():
    try:
        raise ExceptionGroup('abc', [ValueError(), TypeError()])
    except* TypeError:
        1 / 0

def test_error_in_exception_handler():
    a = 1
    try:
        error_in_handler()
    except ExceptionGroup as e:
        assert repr(e) == "ExceptionGroup('', [ZeroDivisionError('division by zero'), ExceptionGroup('abc', [ValueError()])])"
        # TODO what's wrong with the context?
        #assert repr(e.exceptions[0].__context__) == "ExceptionGroup('abc', [TypeError()])"
    else:
        assert 0, "an ExceptionGroup should be raised"

def test_name_except_star():
    l = []
    value = ValueError()
    typ = TypeError()
    try:
        raise ExceptionGroup('abc', [value, typ])
    except* TypeError as e1:
        assert e1.exceptions[0] is typ
        l.append(1)
    except* ValueError as e2:
        assert e2.exceptions[0] is value
        l.append(2)
    print(l)
    assert l == [1, 2]
    with raises(UnboundLocalError):
        e1
    with raises(UnboundLocalError):
        e2

def test_try_star_name_raise_in_except_handler():
    l = []
    value = ValueError()
    typ = TypeError()
    try:
        try:
            raise ExceptionGroup('abc', [value, typ])
        except* TypeError as e1:
            1 / 0
    except Exception as e:
        assert "ZeroDivisionError" in repr(e)
    with raises(UnboundLocalError):
        e1

def maybe_raise_typeerror(x):
    if x:
        raise TypeError

def try_except_star_with_else(x):
    try:
        maybe_raise_typeerror(x)
    except* TypeError:
        a = 1
    else:
        a = 2
    return a

def test_try_except_star_with_else():
    assert try_except_star_with_else(True) == 1
    assert try_except_star_with_else(False) == 2

def test_return_break_continue_in_except_group_handler():
    for kw in "return break continue".split():
        src = f"""\
def try_except_star_with_else_direct_return(x):
    try:
        pass
    except* TypeError:
        {kw}
    """
        with raises(SyntaxError) as info:
            exec(src)
        assert str(info.value).startswith(f"'{kw}' cannot appear in an except* block")

def maybe_raise(err):
    if err:
        raise err
    

def with_finally(l, err):
    try:
        maybe_raise(err)
    except* TypeError:
        l.append(1)
    except* ValueError:
        l.append(2)
    else:
        l.append(3)
    finally:
        l.append(4)

def test_finally():
    l = []
    with_finally(l, None)
    assert l == [3, 4]
    l = []
    with_finally(l, ValueError())
    assert l == [2, 4]
    l = []
    with_finally(l, TypeError())
    assert l == [1, 4]
    l = []
    with_finally(l, ExceptionGroup('abc', [ValueError(), TypeError()]))
    assert l == [1, 2, 4]
    with raises(ZeroDivisionError):
        l = []
        with_finally(l, ZeroDivisionError())
    assert l == [4]

def test_invalid_catching_class():
    for cls, eg in [(int, False), (ExceptionGroup, True), (BaseExceptionGroup, True), ((ValueError, ExceptionGroup), True), ((int, ), False)]:
        with raises(TypeError) as info:
            try:
                1/0
            except* cls:
                pass
        if eg:
            assert "catching ExceptionGroup with except* is not allowed. Use except instead." in str(info.value)
        else:
            assert "catching classes that do not inherit from BaseException is not allowed" in str(info.value)
        assert isinstance(info.value.__context__, ZeroDivisionError)