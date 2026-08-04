# mobile/

[Capacitor](https://capacitorjs.com) shell that wraps the live GitHub
Pages PWA as a native Android / iOS app. Provides app-store-shaped
artifacts when the PWA "Add to Home Screen" path isn't enough.

## Layout

```
capacitor.config.json   Points at https://dmang.com/ca-grid-weather-map/
package.json            @capacitor/{core,android,ios,cli} pins
www/                    Capacitor's required webroot (mostly empty — we
                        point at the hosted PWA, not a bundled webroot)
```

## Build

CI does this — workflows live in `../.github/workflows/`:

| Workflow | Output |
|---|---|
| `build-android.yml` | Unsigned debug `.apk` |
| `build-ios.yml` | Unsigned simulator `.app` |

Both download from the Actions tab. Neither is signed for store
distribution — that needs a keystore (Android) or an Apple Developer
Program account (iOS) and is outside the scope of this project.

## Why this exists

The zero-friction mobile install path is **the PWA itself** — mobile
Safari and Chrome both support "Add to Home Screen" against the live
demo. This shell exists for users who want an app-store-shaped artifact
and as a compile-time regression test on iOS.

**Do not break offline mode.** The service worker (`../sw.js`) is what
makes the PWA work without network — if you change the shell or the
cache list, smoke-test offline behavior in Lighthouse before pushing.
