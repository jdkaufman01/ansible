"""Code coverage utilities."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import re

from .target import (
    walk_module_targets,
    walk_compile_targets,
)

from .util import (
    display,
    ApplicationError,
    common_environment,
    ANSIBLE_TEST_DATA_ROOT,
)

from .util_common import (
    run_command,
)

from .config import (
    CoverageConfig,
    CoverageReportConfig,
)

from .executor import (
    Delegate,
    install_command_requirements,
)

from .data import (
    data_context,
)

COVERAGE_GROUPS = ('command', 'target', 'environment', 'version')
COVERAGE_CONFIG_PATH = os.path.join(ANSIBLE_TEST_DATA_ROOT, 'coveragerc')


def command_coverage_combine(args):
    """Patch paths in coverage files and merge into a single file.
    :type args: CoverageConfig
    :rtype: list[str]
    """
    coverage = initialize_coverage(args)

    modules = dict((t.module, t.path) for t in list(walk_module_targets()) if t.path.endswith('.py'))

    coverage_dir = os.path.join(data_context().results, 'coverage')
    coverage_files = [os.path.join(coverage_dir, f) for f in os.listdir(coverage_dir) if '=coverage.' in f]

    ansible_path = os.path.abspath('lib/ansible/') + '/'
    root_path = data_context().content.root + '/'

    counter = 0
    groups = {}

    if args.all or args.stub:
        # excludes symlinks of regular files to avoid reporting on the same file multiple times
        # in the future it would be nice to merge any coverage for symlinks into the real files
        sources = sorted(os.path.abspath(target.path) for target in walk_compile_targets(include_symlinks=False))
    else:
        sources = []

    if args.stub:
        stub_group = []
        stub_groups = [stub_group]
        stub_line_limit = 500000
        stub_line_count = 0

        for source in sources:
            with open(source, 'r') as source_fd:
                source_line_count = len(source_fd.read().splitlines())

            stub_group.append(source)
            stub_line_count += source_line_count

            if stub_line_count > stub_line_limit:
                stub_line_count = 0
                stub_group = []
                stub_groups.append(stub_group)

        for stub_index, stub_group in enumerate(stub_groups):
            if not stub_group:
                continue

            groups['=stub-%02d' % (stub_index + 1)] = dict((source, set()) for source in stub_group)

    if data_context().content.collection:
        collection_search_re = re.compile(r'/%s/' % data_context().content.collection.directory)
        collection_sub_re = re.compile(r'^.*?/%s/' % data_context().content.collection.directory)
    else:
        collection_search_re = None
        collection_sub_re = None

    for coverage_file in coverage_files:
        counter += 1
        display.info('[%4d/%4d] %s' % (counter, len(coverage_files), coverage_file), verbosity=2)

        original = coverage.CoverageData()

        group = get_coverage_group(args, coverage_file)

        if group is None:
            display.warning('Unexpected name for coverage file: %s' % coverage_file)
            continue

        if os.path.getsize(coverage_file) == 0:
            display.warning('Empty coverage file: %s' % coverage_file)
            continue

        try:
            original.read_file(coverage_file)
        except Exception as ex:  # pylint: disable=locally-disabled, broad-except
            display.error(u'%s' % ex)
            continue

        for filename in original.measured_files():
            arcs = set(original.arcs(filename) or [])

            if not arcs:
                # This is most likely due to using an unsupported version of coverage.
                display.warning('No arcs found for "%s" in coverage file: %s' % (filename, coverage_file))
                continue

            if '/ansible_modlib.zip/ansible/' in filename:
                # Rewrite the module_utils path from the remote host to match the controller. Ansible 2.6 and earlier.
                new_name = re.sub('^.*/ansible_modlib.zip/ansible/', ansible_path, filename)
                display.info('%s -> %s' % (filename, new_name), verbosity=3)
                filename = new_name
            elif collection_search_re and collection_search_re.search(filename):
                new_name = os.path.abspath(collection_sub_re.sub('', filename))
                display.info('%s -> %s' % (filename, new_name), verbosity=3)
                filename = new_name
            elif re.search(r'/ansible_[^/]+_payload\.zip/ansible/', filename):
                # Rewrite the module_utils path from the remote host to match the controller. Ansible 2.7 and later.
                new_name = re.sub(r'^.*/ansible_[^/]+_payload\.zip/ansible/', ansible_path, filename)
                display.info('%s -> %s' % (filename, new_name), verbosity=3)
                filename = new_name
            elif '/ansible_module_' in filename:
                # Rewrite the module path from the remote host to match the controller. Ansible 2.6 and earlier.
                module_name = re.sub('^.*/ansible_module_(?P<module>.*).py$', '\\g<module>', filename)
                if module_name not in modules:
                    display.warning('Skipping coverage of unknown module: %s' % module_name)
                    continue
                new_name = os.path.abspath(modules[module_name])
                display.info('%s -> %s' % (filename, new_name), verbosity=3)
                filename = new_name
            elif re.search(r'/ansible_[^/]+_payload(_[^/]+|\.zip)/__main__\.py$', filename):
                # Rewrite the module path from the remote host to match the controller. Ansible 2.7 and later.
                # AnsiballZ versions using zipimporter will match the `.zip` portion of the regex.
                # AnsiballZ versions not using zipimporter will match the `_[^/]+` portion of the regex.
                module_name = re.sub(r'^.*/ansible_(?P<module>[^/]+)_payload(_[^/]+|\.zip)/__main__\.py$', '\\g<module>', filename).rstrip('_')
                if module_name not in modules:
                    display.warning('Skipping coverage of unknown module: %s' % module_name)
                    continue
                new_name = os.path.abspath(modules[module_name])
                display.info('%s -> %s' % (filename, new_name), verbosity=3)
                filename = new_name
            elif re.search('^(/.*?)?/root/ansible/', filename):
                # Rewrite the path of code running on a remote host or in a docker container as root.
                new_name = re.sub('^(/.*?)?/root/ansible/', root_path, filename)
                display.info('%s -> %s' % (filename, new_name), verbosity=3)
                filename = new_name
            elif '/.ansible/test/tmp/' in filename:
                # Rewrite the path of code running from an integration test temporary directory.
                new_name = re.sub(r'^.*/\.ansible/test/tmp/[^/]+/', root_path, filename)
                display.info('%s -> %s' % (filename, new_name), verbosity=3)
                filename = new_name

            if group not in groups:
                groups[group] = {}

            arc_data = groups[group]

            if filename not in arc_data:
                arc_data[filename] = set()

            arc_data[filename].update(arcs)

    output_files = []
    invalid_path_count = 0
    invalid_path_chars = 0

    coverage_file = os.path.join(data_context().results, 'coverage', 'coverage')

    for group in sorted(groups):
        arc_data = groups[group]

        updated = coverage.CoverageData()

        for filename in arc_data:
            if not os.path.isfile(filename):
                if collection_search_re and collection_search_re.search(filename) and os.path.basename(filename) == '__init__.py':
                    # the collection loader uses implicit namespace packages, so __init__.py does not need to exist on disk
                    continue

                invalid_path_count += 1
                invalid_path_chars += len(filename)

                if args.verbosity > 1:
                    display.warning('Invalid coverage path: %s' % filename)

                continue

            updated.add_arcs({filename: list(arc_data[filename])})

        if args.all:
            updated.add_arcs(dict((source, []) for source in sources))

        if not args.explain:
            output_file = coverage_file + group
            updated.write_file(output_file)
            output_files.append(output_file)

    if invalid_path_count > 0:
        display.warning('Ignored %d characters from %d invalid coverage path(s).' % (invalid_path_chars, invalid_path_count))

    return sorted(output_files)


def command_coverage_report(args):
    """
    :type args: CoverageReportConfig
    """
    output_files = command_coverage_combine(args)

    for output_file in output_files:
        if args.group_by or args.stub:
            display.info('>>> Coverage Group: %s' % ' '.join(os.path.basename(output_file).split('=')[1:]))

        options = []

        if args.show_missing:
            options.append('--show-missing')

        if args.include:
            options.extend(['--include', args.include])

        if args.omit:
            options.extend(['--omit', args.omit])

        env = common_environment()
        env.update(dict(COVERAGE_FILE=output_file))
        run_command(args, env=env, cmd=['coverage', 'report', '--rcfile', COVERAGE_CONFIG_PATH] + options)


def command_coverage_html(args):
    """
    :type args: CoverageConfig
    """
    output_files = command_coverage_combine(args)

    for output_file in output_files:
        dir_name = os.path.join(data_context().results, 'reports', os.path.basename(output_file))
        env = common_environment()
        env.update(dict(COVERAGE_FILE=output_file))
        run_command(args, env=env, cmd=['coverage', 'html', '--rcfile', COVERAGE_CONFIG_PATH, '-i', '-d', dir_name])


def command_coverage_xml(args):
    """
    :type args: CoverageConfig
    """
    output_files = command_coverage_combine(args)

    for output_file in output_files:
        xml_name = os.path.join(data_context().results, 'reports', '%s.xml' % os.path.basename(output_file))
        env = common_environment()
        env.update(dict(COVERAGE_FILE=output_file))
        run_command(args, env=env, cmd=['coverage', 'xml', '--rcfile', COVERAGE_CONFIG_PATH, '-i', '-o', xml_name])


def command_coverage_erase(args):
    """
    :type args: CoverageConfig
    """
    initialize_coverage(args)

    coverage_dir = os.path.join(data_context().results, 'coverage')

    for name in os.listdir(coverage_dir):
        if not name.startswith('coverage') and '=coverage.' not in name:
            continue

        path = os.path.join(coverage_dir, name)

        if not args.explain:
            os.remove(path)


def initialize_coverage(args):
    """
    :type args: CoverageConfig
    :rtype: coverage
    """
    if args.delegate:
        raise Delegate()

    if args.requirements:
        install_command_requirements(args)

    try:
        import coverage
    except ImportError:
        coverage = None

    if not coverage:
        raise ApplicationError('You must install the "coverage" python module to use this command.')

    return coverage


def get_coverage_group(args, coverage_file):
    """
    :type args: CoverageConfig
    :type coverage_file: str
    :rtype: str
    """
    parts = os.path.basename(coverage_file).split('=', 4)

    if len(parts) != 5 or not parts[4].startswith('coverage.'):
        return None

    names = dict(
        command=parts[0],
        target=parts[1],
        environment=parts[2],
        version=parts[3],
    )

    group = ''

    for part in COVERAGE_GROUPS:
        if part in args.group_by:
            group += '=%s' % names[part]

    return group
