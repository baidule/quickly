#! /usr/bin/env python3

# This file is part of QMLify, the build tool for Quickly.
#
# QMLify is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# QMLify is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with QMLify.  If not, see <http://www.gnu.org/licenses/>.

import sys, re, os, os.path
import subprocess
from shutil import copy
import argparse
import json

available_modules = {}

polyfills = [
    'WeakMap', 'Map', 'WeakSet', 'Set', 'Symbol', 'Reflect',
    'Promise', 'fetch', 'Request', 'Response', 'Headers'
]

post_header = '''
var __filename = Qt.resolvedUrl('{}').substring(7);
var __dirname = __filename.substring(0, __filename.lastIndexOf('/'));

var module = {{ exports: {{}} }};
var exports = module.exports;
var global = {{}};
'''

require_as = r'var ([_\w\d]+) = require\([\'\"]([^\'\"]+)[\'\"]\);\n'
require = r'require\([\'\"]([^\'\"]+)[\'\"]\)'
require_effects = r'\nrequire\([\'\"]([^\'\"]+)[\'\"]\);\n'
export_import = r'Object.defineProperty\(exports, \'(.+)\', \{\n\s*enumerable: true,\n\s*get: function get\(\) \{\n\s*return (.*).\1;\n\s*\}\n\s*\}\);'
export_default_import = r'exports\.(.*) = (.*)\.default;\n'

def find_module_files():
    qml_dir = subprocess.check_output(['qmake', '-query', 'QT_INSTALL_QML']).decode('utf-8').strip()
    modules = []
    for root, dirs, files in os.walk(qml_dir):
        for file in files:
            if file == 'package.yml':
                 modules.append(os.path.join(root, file))
    return modules

def build_modules_map():
    for filename in find_module_files():
        yaml = load_yaml(filename)
        for name, qml_import in yaml.get('exports', {}).items():
            # TODO: Add duplicate checking
            # TODO: Only allow QML modules to export types in the own module
            available_modules[name] = qml_import

def load_yaml(fileName):
    from yaml import load
    try:
        from yaml import CLoader as Loader
    except ImportError:
        from yaml import Loader
    stream = open(fileName, "r")
    return load(stream, Loader=Loader)

def save_yaml(fileName, data):
    import yaml
    with open(fileName, 'w') as file:
        file.write(yaml.dump(data, default_flow_style=False))

class DependencyCycle(BaseException):
    def __init__(self, chain):
        super().__init__()
        self.chain = chain

class QMLify(object):
    def __init__(self, build_dir, package_info, use_polyfills=True, use_babel=True):
        self.build_dir = build_dir
        self.use_polyfills = use_polyfills
        self.use_babel = use_babel
        self.dependency_map = {}
        self.modules = {}
        self.base_dir = os.getcwd()
        self.files = {}

        if isinstance(package_info, str):
            self.package_info = load_yaml(package_info)
        else:
            self.package_info = package_info

        self.load_cache()

    def print_info(self):
        json = {name: file.info for name, file in self.files.items()}
        from pprint import pprint
        print(json)

    def check_dependency(self, name, target, chain=None):
        if name not in self.dependency_map:
            return
        dependencies = self.dependency_map[name]

        if chain is None:
            chain = [target, name]

        for dep in dependencies:
            new_chain = chain + [dep]
            if dep == target:
                raise DependencyCycle(new_chain)
            else:
                self.check_dependency(dep, target, new_chain)

    def register_dependency(self, filename, dependency):
        if filename not in self.dependency_map:
            self.dependency_map[filename] = []
        self.dependency_map[filename].append(dependency)

        self.check_dependency(dependency, filename)

    def build(self, path, base_dir=None):
        if os.path.isdir(path):
            for dirname, subdirs, files in os.walk(path):
                if os.path.exists(self.build_dir) and os.path.samefile(self.build_dir, dirname):
                    continue
                for filename in files:
                    self.build(os.path.join(dirname, filename), path)
        else:
            if base_dir is None:
                base_dir = os.path.dirname(path)
            file = QMLifyFile(self, path, base_dir)
            file.build()
            return file

    def save_cache(self):
        save_yaml('.qmlify_cache', dict(files=self.cache))

    def load_cache(self):
        if os.path.exists('.qmlify_cache'):
            self.cache = load_yaml('.qmlify_cache').get('files', {})
        else:
            self.cache = {}

class QMLifyFile(object):
    def __init__(self, qmlify, filename, base_dir, build_dir=None, use_babel=None):
        self.qmlify = qmlify
        self.filename = os.path.relpath(filename, os.getcwd())
        self.base_dir = base_dir
        self.dependencies = []
        self.globals = set()
        self.imported_globals = set()

        self.dirname = os.path.dirname(filename)
        self.basename = os.path.basename(filename)
        self.rel_filename = os.path.relpath(filename, base_dir)

        self.use_babel = use_babel if use_babel is not None else qmlify.use_babel

        if build_dir is None:
            build_dir = self.qmlify.build_dir

        self.build_dir = build_dir
        self.out_filename = os.path.join(build_dir, self.rel_filename)
        self.out_dirname = os.path.dirname(self.out_filename)

    @property
    def needs_build(self):
        return (not os.path.exists(self.out_filename) or
                os.path.getmtime(self.filename) > os.path.getmtime(self.out_filename) or
                os.path.getmtime(__file__) > os.path.getmtime(self.out_filename))

    def babelify(self):
        return subprocess.check_output(['babel', self.filename]).decode('utf-8')

    def build(self):
        if not self.needs_build:
            if self.filename.endswith('.js'):
                self.load_cache()
            return

        print(' - ' + self.filename)

        if not os.path.exists(self.out_dirname):
            os.makedirs(self.out_dirname)

        if not self.filename.endswith('.js'):
            copy(self.filename, self.out_filename)
            return

        if self.use_babel:
            self.text = self.babelify()
        else:
            with open(self.filename) as f:
                self.text = f.read()

        self.globals |= set(re.findall(r'global\.([\w\d_-]+)', self.text))

        self.header = '.pragma library\n'
        self.post_header = post_header.format(self.basename) + '\n'
        self.footer = ''

        if self.qmlify.use_polyfills:
            self.add_polyfills()

        self.build_imports()
        self.fix_exports()
        self.export_globals()

        self.header += '\n' + self.post_header.strip() + '\n'

        self.text = self.header + '\n' + self.text + self.footer
        self.text = self.text.strip() + '\n'

        with open(self.out_filename, 'w') as file:
            file.write(self.text)

        self.qmlify.files[self.filename] = self

        self.save_cache()
        self.qmlify.save_cache()

    def save_cache(self):
        self.qmlify.cache[self.filename] = {
            'dependencies': [os.path.relpath(dep, self.dirname) for dep in self.dependencies],
            'globals': list(self.globals)
        }

    def load_cache(self):
        info = self.qmlify.cache[self.filename]
        self.dependencies = [os.path.abspath(os.path.join(self.dirname, dep)) for dep in info['dependencies']]
        self.globals = set(info['globals'])

    def add_polyfills(self):
        # TODO: Do this via require()
        self.header += '.import Quickly 0.1 as QML_Quickly\n'
        self.post_header += 'var _Polyfills = QML_Quickly.Polyfills.global\n'
        for polyfill in polyfills:
            self.post_header += 'var {} = _Polyfills.{}\n'.format(polyfill, polyfill)

    def build_imports(self):
        self.replace(require_as, self.replace_require)
        for match in re.finditer(require_effects, self.text):
            self.replace_require(match)
        self.replace('\n' + require_effects + '\n', '\n\n')
        self.replace(require_effects + '\n', '\n')
        self.replace('\n' + require_effects, '\n')
        self.replace(require_effects, '\n')
        self.replace(require, self.replace_require)

    def fix_exports(self):
        self.replace(export_import, r'var \1 = exports.\1 = \2.\1;')
        self.replace(export_default_import, r'var \1 = exports.\1 = \2.default;')

    def export_globals(self):
        for name in self.globals:
            if name not in self.imported_globals:
                self.footer += 'var {name} = global.{name};\n'.format(name=name)

    def replace(self, regex, replacement):
        self.text = re.sub(regex, replacement, self.text)

    def module_var(self, import_path):
        if import_path.startswith('./'):
            import_path = import_path[2:]
        while import_path.startswith('../'):
            import_path = '_' + import_path[2:]

        return 'QML_' + import_path.replace('/', '_').replace('.', '_').replace('-', '_')

    def replace_require(self, match):
        require_as = len(match.groups()) == 2

        if require_as:
            name = match.group(1)
            import_path = match.group(2)
            import_as = 'QML' + name if name[0] == '_' else 'QML_' + name
        else:
            import_path = match.group(1)
            import_as = self.module_var(import_path)

        require = self.require(import_path, import_as)

        if require_as:
            return 'var {name} = {require};\n'.format(name=name, require=require)
        else:
            return require

    def parse_import(self, import_path):
        module = available_modules[import_path]
        if ' ' in module:
            module, version = module.split(' ')
        if import_path in self.qmlify.package_info.get('dependencies', {}):
            version = self.qmlify.package_info.get('dependencies', {}).get(import_path)

        if version is None:
            print('No version specified and latest version not specified: ' + import_path)

        qml_module, qml_type = module.split('/')

        return (qml_module, qml_type, version)

    def add_dependency(self, dependency, import_as):
        if isinstance(dependency, QMLifyFile):
            for name in dependency.globals:
                if name not in self.imported_globals:
                    self.post_header += 'var {name} = global.{name} = {import_as}.global.{name};\n'.format(name=name, import_as=import_as)
            self.globals |= dependency.globals
            self.imported_globals |= dependency.globals

            filename = dependency.filename
        else:
            filename = dependency

        self.dependencies.append(filename)
        self.qmlify.register_dependency(self.out_filename, filename)

    def require_local(self, filename, import_as):
        if '.js' in filename:
            raise Exception('Don\'t include the .js file extension when importing/requiring: {}'.format(filename))

        filename += '.js'

        dependency = os.path.abspath(os.path.join(self.dirname, filename))

        qmlified_file = QMLifyFile(self.qmlify, dependency, self.base_dir, build_dir=self.build_dir,
                                   use_babel=self.use_babel)
        qmlified_file.build()

        self.add_dependency(qmlified_file, import_as)

        self.header += '.import "{}" as {}\n'.format(filename, import_as)

        return '{}.module.exports'.format(import_as)

    def require_npm(self, import_path, import_as):
        if '/' in import_path:
            module_name, filename = import_path.split('/', 1)
            filename += '.js'
        else:
            module_name = import_path
            filename = None

        if module_name in self.qmlify.modules:
            module = self.qmlify.modules[module_name]
        else:
            module = NPMModule(self.qmlify, module_name)

            if not module.exists:
                return None

            self.qmlify.modules[module_name] = module

        if filename is None:
            filename = module.main_filename
            qmlified_file = module.require()
        else:
            qmlified_file = module.require(filename)

        self.add_dependency(qmlified_file, import_as)

        rel_filename = os.path.relpath(filename, self.out_dirname)

        self.header += '.import "{}" as {}\n'.format(rel_filename, import_as)

        return '{}.module.exports'.format(import_as)

    def require_qpm(self, import_path, import_as):
        # TODO: Implement QPM modules
        return None

    def require_qml(self, import_path, import_as):
        if import_path in available_modules:
            # TODO: Maybe add dependency cycle checking to QML modules
            qml_module, qml_type, version = self.parse_import(import_path)

            self.add_dependency(import_path, import_as)
            self.header += '.import {} {} as {}\n'.format(qml_module, version, import_as)

            return '({name}.{qml_type}.module ? {name}.{qml_type}.module.exports : {name}.{qml_type})'.format(name=import_as, qml_type=qml_type)

    def require(self, import_path, import_as):
        if import_path.startswith('./') or import_path.startswith('../'):
            return self.require_local(import_path, import_as)
        elif '.js' in import_path:
            correct_name = './' + module_name[:-3]
            raise Exception('Did you mean to import/require \'{}\' instead of \'{}\'?'.format(correct_name,
                                                                                              module_name))
        else:
            require = self.require_npm(import_path, import_as)

            if require is None:
                require = self.require_qpm(import_path, import_as)
            if require is None:
                require = self.require_qml(import_path, import_as)

            if require is not None:
                return require
            else:
                if os.path.exists(os.path.join(self.dirname, import_path + '.js')):
                    correct_name = './' + module_name + '.js'
                    raise Exception('Did you mean to import/require \'{}\' instead of \'{}\'?'.format(correct_name,
                                                                                                      module_name))
                else:
                    raise Exception('Module not exported by any QML or QPM modules and not found in node_modules: ' + module_name)

class NPMModule(object):
    def __init__(self, qmlify, name):
        self.qmlify = qmlify
        self.name = name

        if self.exists:
            package_file = os.path.join(self.src_dirname, 'package.json')
            with open(package_file) as f:
                self.package_info = json.load(f)

    @property
    def src_dirname(self):
        return os.path.abspath(os.path.join(self.qmlify.base_dir, 'node_modules', self.name))

    @property
    def dirname(self):
        return os.path.abspath(os.path.join(self.qmlify.build_dir, 'dependencies', self.name))

    @property
    def exists(self):
        return os.path.exists(self.src_dirname)

    @property
    def main(self):
        return self.package_info.get('main', 'index.js')

    @property
    def src_filename(self):
        return os.path.abspath(os.path.join(self.src_dirname, self.main))

    @property
    def main_filename(self):
        return os.path.abspath(os.path.join(self.dirname, self.main))

    def require(self, filename=None):
        if filename is None:
            filename = self.main
        filename = os.path.abspath(os.path.join(self.src_dirname, filename))

        file = QMLifyFile(self.qmlify, filename, self.src_dirname, build_dir=self.dirname, use_babel=False)
        file.build()
        return file

build_modules_map()

class ModulesAction(argparse.Action):

    def __init__(self,
                 option_strings,
                 dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS,
                 help=None):
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            default=default,
            nargs=0,
            help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        print('{:25} {}'.format('ES6 Module Alias', 'Actual QML import'))
        print('-' * 70)
        for name, qml_module in available_modules.items():
            print('{:25} {}'.format(name, qml_module))
        parser.exit()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='QMLify ES6 source code.')
    parser.add_argument('source_files', metavar='source', type=str, nargs='+',
                       help='one or more ES6 source files to compile')
    parser.add_argument('build_dir', metavar='build', type=str,
                        help='the build directory to output built files to')
    parser.add_argument('--no-polyfills', dest='use_polyfills', action='store_false',
                        help='don\'t include the ES6 polyfills')
    parser.add_argument('--no-babel', dest='use_babel', action='store_false',
                        help='don\'t run babel as part of the qmlify steps')
    parser.add_argument('--modules', dest='show_modules', action=ModulesAction,
                        help='show a list of QML modules mapped to ES6 module names')

    args = parser.parse_args()

    build_dir = os.path.abspath(args.build_dir)

    if os.path.exists('package.yml'):
        package_info = 'package.yml'
    else:
        package_info = {}

    qmlify = QMLify(build_dir, package_info, args.use_polyfills, args.use_babel)

    try:
        for filename in args.source_files:
            qmlify.build(filename)
    except DependencyCycle as cycle:
        print('Dependency cycle:\n - {}'.format('\n - '.join([os.path.relpath(path, os.getcwd())
                                                              for path in cycle.chain])))
        sys.exit(1)

    # qmlify.print_info()