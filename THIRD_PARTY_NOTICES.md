# Third-party notices

Original project material identified in [NOTICE.md](NOTICE.md) is governed by
the [Walkie One Open Community License v0.2](LICENSE). This inventory does not
replace or restrict a dependency's own license. The repository does not vendor
these packages: pip installs their distributions, including their license files.

## Runtime dependencies

Versions come from `personal-server/requirements.txt`; license identifiers below
come from the installed distribution metadata. Verify license files and required
notices again when preparing a public or bundled binary release.
Both runtime and development lockfiles pin package versions and download hashes.

| Package | Version | Declared license |
| --- | --- | --- |
| [annotated-doc](https://pypi.org/project/annotated-doc/0.0.5/) | 0.0.5 | MIT |
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | MIT |
| [anyio](https://pypi.org/project/anyio/4.14.2/) | 4.14.2 | MIT |
| [click](https://pypi.org/project/click/8.4.2/) | 8.4.2 | BSD-3-Clause |
| [fastapi](https://pypi.org/project/fastapi/0.141.1/) | 0.141.1 | MIT |
| [h11](https://pypi.org/project/h11/0.16.0/) | 0.16.0 | MIT |
| [idna](https://pypi.org/project/idna/3.18/) | 3.18 | BSD-3-Clause |
| [pydantic](https://pypi.org/project/pydantic/2.13.4/) | 2.13.4 | MIT |
| [pydantic-core](https://pypi.org/project/pydantic-core/2.46.4/) | 2.46.4 | MIT |
| [starlette](https://pypi.org/project/starlette/1.6.0/) | 1.6.0 | BSD-3-Clause |
| [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | PSF-2.0 |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | 0.4.4 | MIT |
| [uvicorn](https://pypi.org/project/uvicorn/0.52.3/) | 0.52.3 | BSD-3-Clause |
| [websockets](https://pypi.org/project/websockets/17.1/) | 17.1 | BSD-3-Clause |

## Development tools and providers

`requirements-dev.txt` pins the test/audit tools and their transitive dependencies
with hashes. These tools run during development/CI; their package distributions
retain their own licenses. GitHub Actions are pinned to source revisions. Gitleaks
is pinned to a version and verified archive digest. Recheck notices before
redistributing any tool or bundling the Python environment.

Static analysis uses Bandit 1.9.4 (Apache-2.0), with PyYAML 6.0.3 (MIT),
stevedore 5.9.1 (Apache-2.0), and the already locked Rich 15.0.0 (MIT). Their
license identifiers were checked against the installed distribution metadata.
They are installed from their published packages, not vendored here.

Claude, Codex, speech services, and tunnel providers are separate installations or
services. Their names describe compatibility; their binaries, credentials, and
accounts are not included or licensed by this repository.

## Audio fixture

`server-runtime/fallback-error.mp3` is a generated 440 Hz sine tone, not recorded
speech, a cloned voice, or a third-party recording. It is used for the simulated
rehearsal and as a fallback signal when speech is unavailable. It was generated
with the following command (the encoder itself is not redistributed):

```sh
ffmpeg -f lavfi -i 'sine=frequency=440:duration=0.3:sample_rate=24000' \
  -ac 1 -c:a libmp3lame -b:a 48k -map_metadata -1 -write_xing 0 -id3v2_version 0 \
  server-runtime/fallback-error.mp3
```

No firmware vendor source, native application framework, private signing material,
or company speech recording is included in this initial snapshot. Review those
components separately if they are added later.
