
#include "Python.h"
//#define ISSUE_2482

typedef struct {
    PyObject_HEAD
    // Some extra storage:
    char blank[500];
} instance;

static PyObject * get_basicsize(PyObject *self, PyObject * arg)
{
    return PyLong_FromLong(((PyTypeObject*)arg)->tp_basicsize);
}

const char *name0 = "issue2482_object";
const char *name1 = "issue2482_object_base";
static
PyObject *make_object_base_type(void) {

    PyHeapTypeObject *heap_type = (PyHeapTypeObject *) PyType_Type.tp_alloc(&PyType_Type, 0);
    heap_type->ht_name = PyUnicode_FromString("ht_object0");

    PyTypeObject *type = &heap_type->ht_type;
    if (!heap_type) return NULL;
    type->tp_name = name1;
    type->tp_base = &PyBaseObject_Type;
    type->tp_basicsize = sizeof(instance);
    type->tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HEAPTYPE;

    if (PyType_Ready(type) < 0)
        return NULL;

    return (PyObject *) heap_type;
};

static PyMethodDef issue2482_functions[] = {
    {"get_basicsize", (PyCFunction)get_basicsize, METH_O, NULL},
    {NULL,        NULL}    /* Sentinel */
};

#if PY_MAJOR_VERSION >= 3
static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    "issue2482",
    "Module Doc",
    -1,
    issue2482_functions, 
    NULL,
    NULL,
    NULL,
    NULL,
};
#define INITERROR return NULL

/* Initialize this module. */
#ifdef __GNUC__
extern __attribute__((visibility("default")))
#else
extern __declspec(dllexport)
#endif

PyMODINIT_FUNC
PyInit_issue2482(void)

#else

#define INITERROR return

/* Initialize this module. */
#ifdef __GNUC__
extern __attribute__((visibility("default")))
#else
extern __declspec(dllexport)
#endif

PyMODINIT_FUNC
initissue2482(void)
#endif
{
#if PY_MAJOR_VERSION >= 3
    PyObject *module = PyModule_Create(&moduledef);
#else
    PyObject *module = Py_InitModule("issue2482", issue2482_functions);
#endif
    PyHeapTypeObject *heap_type;
    PyTypeObject *type;
    PyObject * base;
    if (module == NULL)
        INITERROR;

    heap_type = (PyHeapTypeObject *) PyType_Type.tp_alloc(&PyType_Type, 0);
    if (!heap_type) INITERROR;
    heap_type->ht_name = PyUnicode_FromString("ht_object1");

    type = &heap_type->ht_type;
    type->tp_name = name0;

    base = make_object_base_type();
    if (! base) INITERROR;
    Py_INCREF(base);
    type->tp_base = (PyTypeObject *) base;
    type->tp_basicsize = ((PyTypeObject *) base)->tp_basicsize;
    type->tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HEAPTYPE | Py_TPFLAGS_CHECKTYPES;

    if (PyType_Ready(type) < 0) INITERROR;

    PyModule_AddObject(module, name0, (PyObject *) type);
};
