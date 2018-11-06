import ast
import importlib
import logging
import pkgutil
import wrapt

log = logging.getLogger(__name__)

SUPPORTED_MODULES = (
    'botocore',
    'pynamodb',
    'requests',
    'sqlite3',
    'mysql',
    'httplib',
    'pymongo',
    'psycopg2',
)

NO_DOUBLE_PATCH = (
    'botocore',
    'pynamodb',
    'requests',
    'sqlite3',
    'mysql',
    'pymongo',
    'psycopg2',
)

_PATCHED_MODULES = set()


def patch_all(double_patch=False):
    if double_patch:
        patch(SUPPORTED_MODULES, raise_errors=False)
    else:
        patch(NO_DOUBLE_PATCH, raise_errors=False)


def _is_valid_import(path):
    return bool(pkgutil.get_loader(path))


def patch(modules_to_patch, raise_errors=True):
    modules = set()
    for module_to_patch in modules_to_patch:
        # boto3 depends on botocore and patching botocore is sufficient
        if module_to_patch == 'boto3':
            modules.add('botocore')
        # aioboto3 depends on aiobotocore and patching aiobotocore is sufficient
        # elif module_to_patch == 'aioboto3':
        #     modules.add('aiobotocore')
        # pynamodb requires botocore to be patched as well
        elif module_to_patch == 'pynamodb':
            modules.add('botocore')
            modules.add(module_to_patch)
        else:
            modules.add(module_to_patch)

    unsupported_modules = modules - set(SUPPORTED_MODULES)
    native_modules = modules - unsupported_modules

    external_modules = set(module for module in unsupported_modules if _is_valid_import(module.replace('.', '/')))
    unsupported_modules = unsupported_modules - external_modules

    if unsupported_modules:
        raise Exception('modules %s are currently not supported for patching'
                        % ', '.join(unsupported_modules))

    for m in native_modules:
        _patch_module(m, raise_errors)

    for m in external_modules:
        _external_recursive_patch(m)


def _patch_module(module_to_patch, raise_errors=True):
    try:
        _patch(module_to_patch)
    except Exception:
        if raise_errors:
            raise
        log.debug('failed to patch module %s', module_to_patch)


def _patch(module_to_patch):

    path = 'aws_xray_sdk.ext.%s' % module_to_patch

    if module_to_patch in _PATCHED_MODULES:
        log.debug('%s already patched', module_to_patch)
        return

    imported_module = importlib.import_module(path)
    imported_module.patch()

    _PATCHED_MODULES.add(module_to_patch)
    log.info('successfully patched module %s', module_to_patch)


def _xray_traced(wrapped, instance, args, kwargs):
    from aws_xray_sdk.core import xray_recorder

    with xray_recorder.capture(name=wrapped.__name__):
        return wrapped(*args, **kwargs)


class XRayPatcherVisitor(ast.NodeVisitor):
    def __init__(self, module):
        self.module = module
        self._current_class = None

    def visit_FunctionDef(self, node):
        name = '{}.{}'.format(self._current_class, node.name) if self._current_class else node.name
        wrapt.wrap_function_wrapper(
            self.module,
            name,
            _xray_traced
        )

    def visit_ClassDef(self, node):
        self._current_class = node.name
        self.generic_visit(node)
        self._current_class = None


def _patch_file(module, f):
    if module in _PATCHED_MODULES:
        log.debug('%s already patched', module)
        return

    with open(f) as open_file:
        tree = ast.parse(open_file.read())
    XRayPatcherVisitor(module).visit(tree)

    _PATCHED_MODULES.add(module)
    log.info('successfully patched module %s', module)


def _external_recursive_patch(module, module_path=None):
    if not module_path:
        module_path = module.replace('.', '/')

    latest_loader = None
    for loader, submodule, is_module in pkgutil.iter_modules([pkgutil.get_loader(module_path)]):
        latest_loader = loader

        submod = '.'.join([loader.path, submodule])
        submodule_path = '/'.join([loader.path, submodule])
        if is_module:
            _external_recursive_patch(submod, submodule_path)
        else:
            _patch_file(submod, '{}.py'.format(submodule_path))

    if latest_loader:
        _patch_file(module, '{}/__init__.py'.format(latest_loader.path))
