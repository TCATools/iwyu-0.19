#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
##===--- iwyu_tool.py -----------------------------------------------------===##
#
#                     The LLVM Compiler Infrastructure
#
# This file is distributed under the University of Illinois Open Source
# License. See LICENSE.TXT for details.
#
##===----------------------------------------------------------------------===##

from __future__ import print_function
import os
import re
import sys
import json
import time
import shlex
import copy
import tempfile
import subprocess

source_dir = os.getenv("SOURCE_DIR")
pwd = os.getcwd()
CORRECT_RE = re.compile(r'^\((.*?) has correct #includes/fwd-decls\)$')
SHOULD_ADD_RE = re.compile(r'^(.*?) should add these lines:$')
ADD_RE = re.compile('^(.*?) +// (.*)$')
SHOULD_REMOVE_RE = re.compile(r'^(.*?) should remove these lines:$')
FULL_LIST_RE = re.compile(r'The full include-list for (.*?):$')
END_RE = re.compile(r'^---$')
LINES_RE = re.compile(r'^- (.*?)  // lines ([0-9]+)-[0-9]+$')


GENERAL, ADD, REMOVE, LIST = range(4)


def clang_formatter(output):
    """ Process iwyu's output into something clang-like. """
    formatted = []
    issues = []

    state = (GENERAL, None)
    for line in output.splitlines():
        match = CORRECT_RE.match(line)
        if match:
            formatted.append('%s:1:1: note: #includes/fwd-decls are correct' %
                             match.groups(1))
            continue
        match = SHOULD_ADD_RE.match(line)
        if match:
            state = (ADD, match.group(1))
            continue
        match = SHOULD_REMOVE_RE.match(line)
        if match:
            state = (REMOVE, match.group(1))
            continue
        match = FULL_LIST_RE.match(line)
        if match:
            state = (LIST, match.group(1))
        elif END_RE.match(line):
            state = (GENERAL, None)
        elif not line.strip():
            continue
        elif state[0] == GENERAL:
            formatted.append(line)
        elif state[0] == ADD:
            match = ADD_RE.match(line)
            if match:
                path = state[1]
                line_num = 1
                column = 1
                add_tmp = copy.copy(match.group(1))
                add_tmp = add_tmp.replace('\"', '')
                add_tmp = add_tmp.replace(source_dir + r'/', '')
                msg = "add '" + add_tmp + "' (" + match.group(2) + ")"
                issues.append({"path": path, "rule": "iwyu-add", "msg": msg, "line": line_num, "column": column})
                formatted.append("%s:1:1: error: add '%s' (%s)" %
                                 (state[1], match.group(1), match.group(2)))
            else:
                path = state[1]
                line_num = 1
                column = 1
                add_tmp = copy.copy(line)
                add_tmp = add_tmp.replace('\"', '')
                add_tmp = add_tmp.replace(source_dir + r'/', '')
                msg = "add '" + add_tmp + "'"
                issues.append({"path": path, "rule": "iwyu-add", "msg": msg, "line": line_num, "column": column})
                formatted.append("%s:1:1: error: add '%s'" % (state[1], line))
        elif state[0] == REMOVE:
            match = LINES_RE.match(line)
            line_no = match.group(2) if match else '1'
            column = 1
            path = state[1]
            msg = "superfluous '" + match.group(1) + "'"
            issues.append({"path": path, "rule": "iwyu-remove", "msg": msg, "line": line_no, "column": column})
            formatted.append("%s:%s:1: error: superfluous '%s'" %
                             (state[1], line_no, match.group(1)))

    return issues, os.linesep.join(formatted)


if sys.platform.startswith('win'):
    # Case-insensitive match on Windows
    def normcase(s):
        return s.lower()
else:
    def normcase(s):
        return s


def is_subpath_of(path, parent):
    """ Return True if path is equal to or fully contained within parent.

    Assumes both paths are canonicalized with os.path.realpath.
    """
    parent = normcase(parent)
    path = normcase(path)

    if path == parent:
        return True

    if not path.startswith(parent):
        return False

    # Now we know parent is a prefix of path, but they only share lineage if the
    # difference between them starts with a path separator, e.g. /a/b/c/file
    # is not a parent of /a/b/c/file.cpp, but /a/b/c and /a/b/c/ are.
    parent = parent.rstrip(os.path.sep)
    suffix = path[len(parent):]
    return suffix.startswith(os.path.sep)


def is_msvc_driver(compile_command):
    """ Return True if compile_command matches an MSVC CL-style driver. """
    compile_command = normcase(compile_command)

    if compile_command.endswith('cl.exe'):
        # Native MSVC compiler or clang-cl.exe
        return True

    if compile_command.endswith('clang-cl'):
        # Cross clang-cl on non-Windows
        return True

    return False


def win_split(cmdline):
    """ Minimal implementation of shlex.split for Windows following
    https://msdn.microsoft.com/en-us/library/windows/desktop/17w5ykft.aspx.
    """
    def split_iter(cmdline):
        in_quotes = False
        backslashes = 0
        arg = ''
        for c in cmdline:
            if c == '\\':
                # MSDN: Backslashes are interpreted literally, unless they
                # immediately precede a double quotation mark.
                # Buffer them until we know what comes next.
                backslashes += 1
            elif c == '"':
                # Quotes can either be an escaped quote or the start of a quoted
                # string. Paraphrasing MSDN:
                # Before quotes, place one backslash in the arg for every pair
                # of leading backslashes. If the number of backslashes is odd,
                # retain the double quotation mark, otherwise interpret it as a
                # string delimiter and switch state.
                arg += '\\' * (backslashes // 2)
                if backslashes % 2 == 1:
                    arg += c
                else:
                    in_quotes = not in_quotes
                backslashes = 0
            elif c in (' ', '\t') and not in_quotes:
                # MSDN: Arguments are delimited by white space, which is either
                # a space or a tab [but only outside of a string].
                # Flush backslashes and return arg bufferd so far, unless empty.
                arg += '\\' * backslashes
                if arg:
                    yield arg
                    arg = ''
                backslashes = 0
            else:
                # Flush buffered backslashes and append.
                arg += '\\' * backslashes
                arg += c
                backslashes = 0

        if arg:
            arg += '\\' * backslashes
            yield arg

    return list(split_iter(cmdline))


def split_command(cmdstr):
    """ Split a command string into a list, respecting shell quoting. """
    if sys.platform.startswith('win'):
        # shlex.split does not work for Windows command-lines, so special-case
        # to our own implementation.
        cmd = win_split(cmdstr)
    else:
        cmd = shlex.split(cmdstr)

    return cmd


def find_include_what_you_use():
    """ Find IWYU executable and return its full pathname. """
    if 'IWYU_BINARY' in os.environ:
        return os.environ.get('IWYU_BINARY')

    # TODO: Investigate using shutil.which when Python 2 has passed away.
    executable_name = 'include-what-you-use'
    if sys.platform.startswith('linux'):
        sys_path = os.environ['PATH']
        tool_bin = pwd + r'/bin'
        os.environ['PATH'] = sys_path + ':' + tool_bin
        lib_dir = os.environ.get("LD_LIBRARY_PATH", None)
        if lib_dir:
            os.environ["LD_LIBRARY_PATH"] = pwd + "lib64:" + lib_dir
        else:
            os.environ["LD_LIBRARY_PATH"] = pwd + "lib64"
    if sys.platform.startswith('win'):
        executable_name += '.exe'

    search_path = [os.path.dirname(__file__)]
    search_path += os.environ.get('PATH', '').split(os.pathsep)

    for dirpath in search_path:
        full = os.path.join(dirpath, executable_name)
        if os.path.isfile(full):
            return os.path.realpath(full)

    return None


IWYU_EXECUTABLE = find_include_what_you_use()


class Process(object):
    """ Manages an IWYU process in flight """
    def __init__(self, proc, outfile):
        self.proc = proc
        self.outfile = outfile
        self.output = None

    def poll(self):
        """ Return the exit code if the process has completed, None otherwise.
        """
        return self.proc.poll()

    @property
    def returncode(self):
        return self.proc.returncode

    def get_output(self):
        """ Return stdout+stderr output of the process.

        This call blocks until the process is complete, then returns the output.
        """
        if not self.output:
            self.proc.wait()
            self.outfile.seek(0)
            self.output = self.outfile.read().decode("utf-8")
            self.outfile.close()

        return self.output

    @classmethod
    def start(cls, invocation):
        """ Start a Process for the invocation and capture stdout+stderr. """
        outfile = tempfile.TemporaryFile(prefix='iwyu')
        process = subprocess.Popen(
            invocation.command,
            cwd=invocation.cwd,
            stdout=outfile,
            stderr=subprocess.STDOUT)
        return cls(process, outfile)


KNOWN_COMPILER_WRAPPERS=frozenset([
    "ccache"
])


class Invocation(object):
    """ Holds arguments of an IWYU invocation. """
    def __init__(self, command, cwd):
        self.command = command
        self.cwd = cwd

    def __str__(self):
        return ' '.join(self.command)

    @classmethod
    def from_compile_command(cls, entry, extra_args):
        """ Parse a JSON compilation database entry into new Invocation. """
        if 'arguments' in entry:
            # arguments is a command-line in list form.
            command = entry['arguments']
        elif 'command' in entry:
            # command is a command-line in string form, split to list.
            command = split_command(entry['command'])
        else:
            raise ValueError('Invalid compilation database entry: %s' % entry)

        if command[0] in KNOWN_COMPILER_WRAPPERS:
            # Remove the compiler wrapper from the command.
            command = command[1:]

        # Rewrite the compile command for IWYU
        compile_command, compile_args = command[0], command[1:]
        if is_msvc_driver(compile_command):
            # If the compiler is cl-compatible, let IWYU be cl-compatible.
            extra_args = ['--driver-mode=cl'] + extra_args

        command = [IWYU_EXECUTABLE] + extra_args + compile_args
        return cls(command, entry['directory'])

    def start(self, verbose):
        """ Run invocation and collect output. """
        if verbose:
            print('# %s' % self, file=sys.stderr)

        return Process.start(self)


def fixup_compilation_db(compilation_db):
    """ Canonicalize paths in JSON compilation database. """
    for entry in compilation_db:
        # Convert relative paths to absolute ones if possible, based on the entry's directory.
        if 'directory' in entry and not os.path.isabs(entry['file']):
            entry['file'] = os.path.join(entry['directory'], entry['file'])

        # Expand relative paths and symlinks
        entry['file'] = os.path.realpath(entry['file'])

    return compilation_db


def slice_compilation_db(compilation_db, selection):
    """ Return a new compilation database reduced to the paths in selection. """
    if not selection:
        return compilation_db

    # Canonicalize selection paths to match compilation database.
    selection = [os.path.realpath(p) for p in selection]

    new_db = []
    for path in selection:
        if not os.path.exists(path):
            print('warning: \'%s\' not found on disk.' % path, file=sys.stderr)
            continue

        found = [e for e in compilation_db if is_subpath_of(e['file'], path)]
        if not found:
            print('warning: \'%s\' not found in compilation database.' % path,
                  file=sys.stderr)
            continue

        new_db.extend(found)

    return new_db


def execute(invocations, verbose, jobs, max_load_average=0):
    """ Launch processes described by invocations. """
    exit_code = 0
    issues = []
    if jobs == 1:
        for invocation in invocations:
            proc = invocation.start(verbose)
            issue, output = clang_formatter(proc.get_output())
            issues.extend(issue)
            print(output)
            exit_code = max(exit_code, proc.returncode)
        with open("result.json", "w") as fp:
            json.dump(issues, fp, indent=2)
        return exit_code

    pending = []
    while invocations or pending:
        # Collect completed IWYU processes and print results.
        complete = [proc for proc in pending if proc.poll() is not None]
        for proc in complete:
            pending.remove(proc)
            issue, output = clang_formatter(proc.get_output())
            issues.extend(issue)
            print(output)
            exit_code = max(exit_code, proc.returncode)

        # Schedule new processes if there's room.
        capacity = jobs - len(pending)

        if max_load_average > 0:
            one_min_load_average, _, _ = os.getloadavg()
            load_capacity = max_load_average - one_min_load_average
            if load_capacity < 0:
                load_capacity = 0
            if load_capacity < capacity:
                capacity = int(load_capacity)
                if not capacity and not pending:
                    # Ensure there is at least one job running.
                    capacity = 1

        pending.extend(i.start(verbose) for i in invocations[:capacity])
        invocations = invocations[capacity:]

        # Yield CPU.
        time.sleep(0.0001)
    with open("result.json", "w") as fp:
        json.dump(issues, fp, indent=2)
    return exit_code


def main(compilation_db_path, source_files, verbose, jobs,
         max_load_average, extra_args):
    """ Entry point. """

    if not IWYU_EXECUTABLE:
        print('error: include-what-you-use executable not found',
              file=sys.stderr)
        return 1

    try:
        if os.path.isdir(compilation_db_path):
            compilation_db_path = os.path.join(compilation_db_path,
                                               'compile_commands.json')

        # Read compilation db from disk.
        compilation_db_path = os.path.realpath(compilation_db_path)
        with open(compilation_db_path, 'r') as fileobj:
            compilation_db = json.load(fileobj)
    except IOError as why:
        print('error: failed to parse compilation database: %s' % why,
              file=sys.stderr)
        return 1

    compilation_db = fixup_compilation_db(compilation_db)
    compilation_db = slice_compilation_db(compilation_db, source_files)

    # Transform compilation db entries into a list of IWYU invocations.
    invocations = [
        Invocation.from_compile_command(e, extra_args) for e in compilation_db
    ]

    return execute(invocations, verbose, jobs, max_load_average)

def find_compile_json(search_path):
    g = os.walk(search_path)
    for path, dir_list, file_list in g:
        for file_name in file_list:
            if file_name == 'compile_commands.json':
                return path
    return None

def search_file():
    """ Search c/c++ file and return list """
    g = os.walk(source_dir)
    mark_file = []
    for path, dir_list, file_list in g:
        for file_name in file_list:
            if file_name.endswith(".c") or file_name.endswith(".cpp") or file_name.endswith(".cc"):
                mark_file.append(os.path.join(path, file_name))
    return mark_file

if __name__ == '__main__':
    # 可通过环境变量指定compile_commands.json路径
    if os.environ.get("COMPILE_JSON", None):
        compile_json = os.environ.get("COMPILE_JSON")
    else:
        compile_json = find_compile_json(source_dir)
    if not compile_json:
        print("未能找到compile_commands.json")
        mark_file = search_file()
        scan_files_env = os.getenv("SCAN_FILES")
        if scan_files_env and os.path.exists(scan_files_env):
            with open(scan_files_env, "r") as rf:
                scan_files = json.load(rf)
        obj_file = set(mark_file).intersection(scan_files)
        invocations = []
        for file_name in obj_file:
            command = [IWYU_EXECUTABLE]
            command.append(file_name)
            command.append("-Xiwyu")
            command.append("--no_fwd_decls")
            invocations.append(Invocation(command, source_dir))
        execute(invocations, True, 4, 0)
    else:
        print(compile_json)
        source_file = []
        # Print IWYU commands
        ver = True
        # Number of concurrent subprocesses
        job_number = 4
        load = 0
        extra = ["-Xiwyu", "--no_fwd_decls"]
        main(compile_json, source_file, ver, job_number, load, extra)
