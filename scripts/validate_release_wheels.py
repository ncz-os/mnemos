"""Validate staged add-on core requirements before building images."""
import email
from pathlib import Path
import runpy
import sys
import zipfile
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.markers import default_environment


def validate(directory):
    version = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'mnemos/_version.py'))['__version__']
    wheels = list(Path(directory).glob('*.whl'))
    if not wheels:
        raise ValueError('No staged wheels')
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
            if len(names) != 1:
                raise ValueError('Invalid wheel metadata')
            metadata = email.message_from_bytes(archive.read(names[0]))
        for value in metadata.get_all('Requires-Dist', []):
            requirement = Requirement(value)
            if canonicalize_name(requirement.name) != 'mnemos-core':
                continue
            for machine in ('x86_64', 'aarch64'):
                environment = {**default_environment(), 'platform_machine': machine, 'extra': ''}
                if requirement.marker and not requirement.marker.evaluate(environment):
                    continue
                if requirement.url or version not in requirement.specifier:
                    raise ValueError('core requirement is not satisfied')


if __name__ == '__main__':
    try:
        validate(sys.argv[1])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
