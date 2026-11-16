# Desktop release and install

The desktop shell is a remote-URL wrapper: it carries no web bundle, so a web
release needs no desktop release. The version comes from one place:

```text
desktop/package.json -> version
```

Do not edit `electron-builder.yml` to change the version. The build embeds the
package version in the bundle and artifact names.

## Commands

```bash
make desktop-dist      # DMG + ZIP for arm64/x64 under desktop/dist/oss/
make desktop-status    # repo vs built vs installed version, duplicate apps
make desktop-install   # copy the built app to ~/Applications and remove duplicate .app bundles
make doctor            # includes the desktop version/duplicate check
```

Equivalent direct commands:

```bash
cd desktop && pnpm dist
python3 scripts/desktop_release.py status
python3 scripts/desktop_release.py install
```

Artifacts:

```text
desktop/dist/oss/Conductor-oss-<version>-arm64.dmg / .zip
desktop/dist/oss/Conductor-oss-<version>-x64.dmg   / .zip
```

## One installed Conductor

electron-builder leaves an unpacked `Conductor.app` under
`desktop/dist/oss/<arch>/`. Launch Services/Spotlight can index it as a second
Conductor even though it is only a build artifact.

`desktop-install`:

1. verifies the built bundle's id/version;
2. refuses if the app is running;
3. copies the new bundle with `ditto`, replacing
   `~/Applications/Conductor.app` with a rollback backup;
4. removes the duplicate unpacked build bundles (DMG/ZIP stay as artifacts).

Use `--keep-build-app` if you are debugging the unpacked bundle:

```bash
python3 scripts/desktop_release.py install --keep-build-app
```

## Signing

OSS builds are **ad-hoc signed, not notarized**. `identity: null` makes
electron-builder skip signing, so `desktop/scripts/after-pack.cjs` ad-hoc signs
the app bundle (`codesign --sign -`) before the DMG/ZIP are produced. That keeps
the bundle self-consistent for local installs; it is still not a Gatekeeper
release signature. When `CSC_LINK`/`CSC_NAME` is set, the hook leaves signing to
electron-builder.

A real release needs both signing and notarization:

```bash
CSC_LINK=/path/to/DeveloperID.p12 \
CSC_KEY_PASSWORD=... \
APPLE_ID=... \
APPLE_APP_SPECIFIC_PASSWORD=... \
APPLE_TEAM_ID=... \
  make desktop-dist
```

See `desktop/electron-builder.yml` and `desktop/scripts/build.mjs` for the
certificate/notarization switches. A signed build without notarization is
refused by Gatekeeper the same way an unsigned one is, so do not ship one.

## Version update checklist

1. bump `desktop/package.json` version;
2. `make desktop-dist`;
3. `make desktop-status` (installed version must match the new version);
4. `make desktop-install`;
5. `make desktop-status` -> READY, one app bundle.
