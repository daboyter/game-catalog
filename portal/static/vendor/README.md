# Vendored libraries

Downloaded once and served from here, so the portal needs nothing from the
internet at runtime except game covers. No build step: the files are used
exactly as published.

| File | Library | Version | License | Used for |
|---|---|---|---|---|
| `alpine.min.js` | [Alpine.js](https://alpinejs.dev) | 3.17.5 | MIT | The ⌘K quick find (`templates/_palette.html`) |

To update, replace the file with the new release's `dist/cdn.min.js` and
change the version here and in `templates/base.html`.
