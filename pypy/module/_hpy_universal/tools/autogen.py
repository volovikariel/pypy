import sys
import py
if sys.version_info[0] < 3:
    print('ERROR: autogen.py should be run on top of Python 3.x')
    sys.exit(1)
if len(sys.argv) != 2:
    print('USAGE: autogen.py /path/to/hpy_repo')
    sys.exit(1)

HPY_PATH = sys.argv[1]
sys.path.insert(0, HPY_PATH)

from hpy.tools.autogen.parse import HPyAPI, PUBLIC_API_H, toC
from hpy.tools.autogen.autogenfile import AutoGenFile


class autogen_interp_slots_py(AutoGenFile):
    PATH = 'autogen_interp_slots.py'
    LANGUAGE = 'Python'
    DISCLAIMER = '## DO NOT EDIT THIS FILE, IT IS AUTOGENERATED'

    INCLUDE = ['HPyFunc_reprfunc',
               'HPyFunc_unaryfunc',
               'HPyFunc_binaryfunc',
               'HPyFunc_ssizeargfunc',]

    def generate(self):
        lines = []
        w = lines.append
        #
        w(f'from pypy.module._hpy_universal import llapi, handles')
        w(f'from pypy.module._hpy_universal.interp_slot import W_SlotWrapper')
        w(f'from pypy.module._hpy_universal.state import State')
        w(f'')
        for hpyfunc in self.api.hpyfunc_typedefs:
            if hpyfunc.name not in self.INCLUDE:
                continue
            nargs = len(hpyfunc.params()) - 1 # -1 because we don't want to count ctx
            w(f'class W_SlotWrapper_{hpyfunc.base_name()}(W_SlotWrapper):')
            w(f'    def call(self, space, __args__):')
            w(f'        self.check_args(space, __args__, {nargs})')
            w(f'        func = llapi.cts.cast("{hpyfunc.name}", self.cfuncptr)')
            w(f'        ctx = space.fromcache(State).ctx')
            using = []
            c_params = []
            for i, p in enumerate(hpyfunc.params()[1:]): # ignore ctx
                c_params.append(f'c{i}')
                w(f'        w{i} = __args__.arguments_w[{i}]')
                if toC(p.type) == 'HPy':
                    using.append(f'handles.using(space, w{i}) as c{i}')
                else:
                    w(f'        c{i} = {self.convert_param(i, p)}')
            #
            if using:
                using = ', '.join(using)
                w(f'        with {using}:')
            else:
                w(f'        if 1:')
            #
            c_params = ', '.join(c_params)
            w(f'            c_result = func(ctx, {c_params})')
            w(f'            return {self.convert_return(hpyfunc)}')
            w(f'')
        return '\n'.join(lines)

    def convert_param(self, i, p):
        t = toC(p.type)
        if t == 'HPy_ssize_t':
            return f'space.int_w(space.index(w{i}))'
        assert False, f'Unsupported type: {t}'

    def convert_return(self, hpyfunc):
        t = toC(hpyfunc.return_type())
        if t == 'HPy':
            return 'handles.consume(space, c_result)'
        assert False, f'Unsupported type: {t}'



def main():
    OUTDIR = py.path.local(__file__).dirpath('..')
    api = HPyAPI.parse(PUBLIC_API_H)
    for cls in (autogen_interp_slots_py,):
        #print(cls(api).generate())
        cls(api).write(OUTDIR)



if __name__ == '__main__':
    main()
