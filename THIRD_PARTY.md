# Third-party notices — `pi-herdr-shop`

## pi-intercom 0.13.0 (MIT)

The Shop built-in transport (`transport/`) is a port of the minimum necessary
transport code from `pi-intercom@0.13.0`. No upstream package is installed as a
dependency and no upstream broker is contacted.

| Item | Value |
| --- | --- |
| Package | `pi-intercom@0.13.0` |
| License | MIT (`LICENSE.pi-intercom`, sha256 `2d20dfacd9742706e564470dc77438608a1e54b0ed46959f080709389209093c`) |
| Copyright | Copyright (c) 2026 Nico Bailon |
| Tarball | `https://registry.npmjs.org/pi-intercom/-/pi-intercom-0.13.0.tgz` |
| Tarball sha256 | `1d89bd31ca63cccd82a5c950cc043bca99e17e4664df7fd99f16859a0442279f` |
| Tarball sha512 | `sha512-+QjKJRAEhrgQZj4+M9OW/8unRLvCzeCp0K66lZmbS5/me0fsClXRtANBgM6mY+EoX+Fcd+qBE8dTThp8+ND//g==` |
| Upstream ref | npm `gitHead 199279ae861bf53ce014809fb2a03337538ae13e` |
| Ported files | see `transport/NOTICE.md` for per-file origin hashes and the exact modifications |

Everything outside the listed ported files was written for this package; the
removed upstream behaviours (extension/UI layer, outbox/namespace bus, mailbox
redelivery, ask inference, cwd/name target guessing, supersede) are documented
in `transport/NOTICE.md` and enforced by `tests/transport_isolation.test.ts`.

The MIT terms require retaining the copyright notice; `LICENSE.pi-intercom` is
shipped unchanged and each ported file carries an attribution header.
