#!/usr/bin/python3

'''This is a prototype script for removing "from module import *"
statements from a Python source file, replacing them with a simple
"import module" and all the names derived from that module with
"module.name".  While it depends on Macropy, it doesn't use any macros
itself, only some code for Python ASTs.  It uses static analysis on
the files it's passed as aguments to find import statements, but
itself imports any modules or packages those files need to import.
Thus, it must be able to find all the imports on the module search
path.  If you want to operate on modules or packages outside the usual
search path, use the --path switch to add a path to sys.path.  It
isn't intended to handle all possible Python code, only to take most
of the work out of the most common cases: expect files to need manual
cleanup afterwards.  It particular, it can't:

* Remove all branches for conditional imports.  Typically, it will
  pick the last branch and ignore the rest.

* Remove dynamic imports.

* Change names from modules imported inside class or function scopes.
  Scoped imports may lead to names outside that scope being
  changed incorrectly.

* Handle multiple import statments on one line that are separated
  using semicolons.

By default it removes only "import *" lines, though passing --all will
cause it to operate on all "from module import foo" imports.

'''

# There are two plausible approaches here, one of which uses static
# analysis all the way and the other which tries to take some
# short-cuts by running code.  To use static analysis all the way
# requires import hooks so you can parse the file that would otherwise
# be imported into an AST to carry out static analysis.  This is
# probably easier in Python 3.3 because you can use the finders in
# importlib, while in Python 2 this probably requires using some kind
# of custom loader.  Otherwise, you use static analysis on the file
# itself but do some kind of import execution to figure out where
# names came from.  In this case, I'm using inspect.getmodule().

from __future__ import print_function

import argparse
import ast
import collections
import importlib
import inspect
import logging
import pathlib
import re
import sys
import traceback

import macropy.core.walkers as walkers
import macropy.core.analysis as analysis

def import_module(import_from, package=None):
    '''Imports a module and, if necessary, its containing package.
    
    Args:
        import_from: ImportFrom AST node.
        package: The module's package, if any, as a string.  Only necessary
            for relative imports.

    Returns:
        The imported module.
    '''
    if import_from.level == 0:
        return importlib.import_module(import_from.module)
    else:
        module = import_from.module if import_from.module else ''
        logging.info('Relative Import: %s, %s', module, package)
        return importlib.import_module(
            '.'*import_from.level + module, package)

def find_origin(module, name):
    '''Tries to find the origin of an imported name.

    Failing that, it assumes the name came from the module it was
    imported from.

    Args:
        module: Module's full (possibly dotted) name as a string.
        name: Name from the module as a string.

    Returns:
       A tuple of the full dotted name and the short name.

    '''
    try:
        return inspect.getmodule(getattr(module, name)).__name__
    except AttributeError:
        return module.__name__


@analysis.Scoped
@walkers.Walker
def remove_from_imports(
    tree, scope, package, imported_names, remove_all, set_ctx, collect, **kws):
    '''Traverses an AST to find import statements and names that need to
    be changed.

    This function recurses through the tree, recording each ImportFrom
    node and Name node that need to be changed and rewriting the tree
    to change ImportFrom nodes to Import nodes and Name nodes to use
    the full dotted names.

    Args:
        tree: Root of the AST to traverse.
        scope: Variables in scope as a dict from names to values, provided
            by macropy.core.analysis.Scoped.
        package: The module's package, if any, as a string.
        imported_names: The names added to the module namespace by imports
            of the type to be removed as a dict with the names as they exist in
            the file mapping to tuples of the line number, column offset, and 
            a tuple of the module and the name as it exists in the module it
            came from.  
        set_ctx: A function passing changed arguments down recursive calls, see
            macropy.core.walkers.Walker.
        collect: Appends a value to a list which will be returned by 
            remove_from_imports.collect(), see macropy.core.walkers.Walker.

    Returns:
        A rewritten AST (using .recurse()), a list of tuples as found in the 
        values of imported_names (using .collect()), or both, using 
        .recurse_collect(), macropy.core.walkers.Walker.

    '''
    logging.debug('AST: %s', ast.dump(tree))
    logging.debug('Scope: %s', scope)

    if isinstance(tree, ast.ImportFrom):
        module = import_module(tree, package)
        origins = set()
        if len(tree.names) == 1 and tree.names[0].name == '*':
            if hasattr(module, '__all__'):
                for name in module.__all__:
                    origin = find_origin(module, name)
                    origins.add(origin)
                    imported_names[name] = (origin, name)
            else:
                for name in dir(module):
                    if not name.startswith('_'):
                        origin = find_origin(module, name)
                        origins.add(origin)
                        imported_names[name] = (origin, name)
        elif remove_all:
            for alias in tree.names:
                origin = find_origin(module, alias.name)
                origins.add(origin)
                if alias.asname:
                    imported_names[alias.asname] = (origin, alias.name)
                else:
                    imported_names[alias.name] = (origin, alias.name)
        else:
            return tree
        logging.info('Imported Names: %s', imported_names)
        set_ctx(imported_names=imported_names)
        return ast.Import([ast.alias(m, None) for m in sorted(origins)])

    elif (isinstance(tree, ast.Name) and isinstance(tree.ctx, ast.Load) and
    tree.id not in scope and tree.id in imported_names):
        logging.info('Lineno: %i', tree.lineno)
        logging.info(ast.dump(tree))
        collect((tree.lineno, tree.col_offset, imported_names[tree.id]))
        return ast.Name(
            '%s.%s' % (imported_names[tree.id][0], tree.id), tree.ctx)


FROM_IMPORT = r'\s*from\s+(%s)\s+import\s+(%s)'
from_future_import = re.compile(FROM_IMPORT % ('__future__', r'[\w, ]+'))
from_import_star = re.compile(FROM_IMPORT % (r'[.\w]+', r'\*'))
from_import = re.compile(FROM_IMPORT % (r'[.\w]+', r'[\w., *]+'))
comment_string_whitespace = re.compile(
    r'''^\s*(#.*|'[^']*'|'{3}.*?'{3}|"[^"]*"|"{3}.*?"{3}|\s)$''')

SCRIPT_COMMENT = '''
# Imports added by remove_from_imports.
'''

def write_changes(original, modules, changes, refactored, remove_all):
    '''Writes a set of changes to a file-like object.

    Args:
        original: The contents of the source file, as a string.
        modules: A set of the modules to be changed.
        changes: A dict of line numbers mapping to tuples of column offset, the
            module name to add, and the name to change itself.
        refactored: A file-like object to write changes to.
        remove_all: If true, refactors all "from module import foo" statements.
    '''
    lines = enumerate(original.splitlines(), 1)

    # This loop catches the leading elements of Python source files,
    # docstrings (string literals, whether split over multiple lines
    # or one line), comments (including shebang lines), from
    # __future__ imports, and white space, echoing them to the output
    # without change.  When it encounters the first line of real code
    # that isn't from a from __future__ import, it should stop.  It
    # should be impossible for any of these preamble lines to contain
    # names that would need to be changed in a syntactically-correct
    # Python file.
    multiline_string = None
    for lineno, line in lines:
        logging.info('Lineno, Multiline: %s, %s', lineno, multiline_string)
        logging.debug(
            'Comment, string, or whitespace: %s',
            comment_string_whitespace.match(line))
        if (comment_string_whitespace.match(line) or
            from_future_import.match(line)):
            pass
        elif multiline_string:
            if multiline_string in line:
                multiline_string = None
        elif line.strip().startswith("'''") or line.strip().startswith('"""'):
            multiline_string = line.strip()[0:3]
        else:
            break
        refactored.write(line + '\n')

    # This writes all the modules needed as 'import module' statements.
    refactored.write('\n')
    refactored.write(SCRIPT_COMMENT)
    refactored.write('\n')
    for module in modules:
        logging.info('import %s', module)
        refactored.write('import %s\n' % module)
    refactored.write('\n')

    # This loop uses the changes dict to write changed lines (as
    # needed) and unchanged lines to the output while skipping all
    # "from module import" lines that are being removed..  Using a
    # while loop with an explicit catch of StopIteration allows it to
    # act on the first line of code, which is the last line found by
    # the previous for loop.
    while True:
        if (from_import_star.match(line) or
            (from_import.match(line) and remove_all)):
            logging.info(from_import.match(line))
        elif lineno in changes:
            new_line = []
            cur_offset = 0
            for offset, module, name in changes[lineno]:
                new_line.extend(
                    [line[cur_offset:offset], '%s.%s' % (module, name)])
                cur_offset = offset + len(name)
            new_line.extend([line[cur_offset:], '\n'])
            logging.info(new_line)
            refactored.write(''.join(new_line))
        else:
            refactored.write(line + '\n')
        try:
            lineno, line = next(lines)
        except StopIteration:
            break


if __name__ == '__main__':
    arg_parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    arg_parser.add_argument(
        'files', nargs='+', help='Files to remove imports from.')
    arg_parser.add_argument(
        '-p', '--path', help='Path to the directory containing the package.')
    arg_parser.add_argument(
        '-o', '--output-dir', type=pathlib.Path,
        help='Put modified files in this directory.  Overrides -w.')
    arg_parser.add_argument(
        '-w', '--write', action='store_true',
        help='Write back modified files, creating backup files in the same directory.')
    arg_parser.add_argument(
        '-a', '--all', action='store_true',
        help='Remove all "from module import foo" statements.')
    arg_parser.add_argument(
        '-v', '--verbose', action='count', default=0, help='Verbose output.')
    args = arg_parser.parse_args()
    if args.path:
        sys.path.append(args.path)
    logging.basicConfig(
        format='%(message)s', level=30-10*args.verbose, stream=sys.stdout)

    for file_name in args.files:
        print('File: %s' % file_name)
        path = pathlib.Path(file_name).resolve()

        # This assumes that the package containing file_name is in
        # sys.path and that the directory containing the package is a
        # direct subdirectory of some sys.path entry.  This has the
        # potential to fail on Python 3.2, 2.7, and earlier because
        # Python requires an __init__.py to recognize a directory as a
        # package if the direct subdirectory isn't a package but
        # contains one.  On the other hand, on Python 3.3+, requiring
        # an __init__.py file can fail to recognize a valid package
        # because it's no longer required.  See my question at
        # https://stackoverflow.com/q/29826934/3857947 There are other
        # possible ways for this to fail, with packages containing an
        # __init__.py that sets __path__ and other dynamic import
        # functionality.  Most of them can be fixed with an
        # appropriate choice of the --path command line argument.
        for python_path in map(pathlib.Path, sys.path):
            try:
                package_path = path.relative_to(python_path)
            except ValueError:
                continue
            package = '.'.join(package_path.parts[:-1])
            break
        else:
            package = None
        print('Package: %s' % package)
        if package:
            try:
                importlib.import_module(package)
            except Exception:
                logging.error(traceback.format_exc())

        with open(file_name, 'r') as f:
            original = f.read()
        try:
            change_list = remove_from_imports.collect(
                ast.parse(original), package=package, imported_names={},
                remove_all=args.all)
        except Exception:
            logging.error(traceback.format_exc())
            continue
        modules = {i[2][0] for i in change_list}
        logging.info(modules)
        changes = collections.defaultdict(list)
        for i in change_list:
            changes[i[0]].append((i[1],) + i[2])
        logging.info(changes)
        if changes:
            try:
                if args.output_dir:
                    with (args.output_dir / path.name).open('w') as refactored:
                        write_changes(
                            original, modules, changes, refactored, args.all)
                elif args.write:
                    path.rename(path.with_name(path.name + '.bak'))
                    with path.open('w') as refactored:
                        write_changes(
                            original, modules, changes, refactored, args.all)
                else:
                    write_changes(
                        original, modules, changes, sys.stdout, args.all)
            except Exception:
                logging.error(traceback.format_exc())

        # analyze_imports.recurse(ast.parse(original))
            # print(ast.dump(tree))
            # Unparser(tree)
            # for lineno, old_line, new_line in zip(
            #         itertools.count(1), original.splitlines(), 
            #         changed.getvalue().splitlines()):
            #     print(lineno, old_line, new_line)
            # for l in difflib.unified_diff(original.splitlines(), o.getvalue().splitlines()):
            #     print(l)
