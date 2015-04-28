## Synopsis

This Python script removes `from module import *` statements from
Python source files, replacing them with `import module` statements
and the imported names with `module.foo`.  Optionally, it can remove
all `from module import foo` statements.

## Code Example

The simplest possible use from a Unix/Linux shell is:
`python remove_from_imports.py foo.py > new_foo.py`

A typical use is:
`find project/ -name '*.py' | xargs python remove_from_imports.py -w`

## Motivation

Many uses of `from module import *` can make it impossible to find
where a name originally came from in any project over a certain size.
This script automates the process of replacing them.

## Installation

This script requires Macropy, but does not use any macros, only AST
analysis like say pylint.

## License

MIT.