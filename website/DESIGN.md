# Quant Workbench website

## Direction

Brand: Quant Workbench.
Main line: 把你的想法，训练成模型。
Campaign line: 训练你的判断。分享你的证据。

The website introduces the local research platform and connects directly to the existing FastAPI model-sharing service. The model gallery distinguishes actual reference results, training templates, and user-published models. No fabricated users, return records, customer logos, or adoption statistics are used.

## GitHub research (2026-10-21)

- https://github.com/supabase/supabase : mature product navigation, documentation and community structure. Reviewed README and product assets. Design reference only; no copied source or brand assets.
- https://github.com/mlflow/mlflow : research workflow, model evaluation and experiment context. Reviewed README. Information architecture reference only.
- https://github.com/pmndrs/react-three-fiber : mature Three.js ecosystem and rendering patterns. Reviewed README and package metadata. This project uses Three.js directly to keep the scene small and self-contained.
- https://github.com/DavidHDev/react-bits : reviewed animated components overview and MIT + Commons Clause license. Visual research only; no source copied.
- https://github.com/saasfly/saasfly : reviewed the enterprise SaaS starter overview and its product/demo structure. The existing FastAPI service was retained to preserve the working model-sharing implementation.
- https://github.com/magicuidesign/magicui : reviewed the MIT-licensed component library overview. Useful alternative for future marketing components; not installed or copied into this implementation.
- https://github.com/mrdoob/three.js : MIT-licensed rendering dependency used for the original optical glass scene.
- https://github.com/motiondivision/motion : MIT-licensed animation library, installed and used for the hero entrance and model reveal, with reduced-motion support.
- https://github.com/lucide-icons/lucide : ISC-licensed icon dependency.
- https://github.com/simple-icons/simple-icons : CC0 Python, PyTorch and Hugging Face brand symbols, stored locally in public/brands. Symbols identify compatibility, not customer endorsement.
- https://github.com/google/fonts/tree/main/ofl/dmsans : locally hosted DM Sans variable font with its SIL Open Font License. Chinese text uses native system fonts. The production page makes no font CDN requests.

GitHub REST metadata requests were rate limited. Public raw repository files were accessible and inspected. No star counts are quoted.

## Hero

Original full-width Three.js sculpture made from 43 optical glass slices, a generated reflection environment, spectral colors and a continuously changing signal surface. Pointer movement changes orientation. Pause and prefers-reduced-motion are supported. A locally exported PNG image provides the non-WebGL fallback. No external video, stock footage or runtime image CDN is needed.

The scene expresses a continuous research and training process. It can be replaced with a prerecorded video without changing the surrounding layout. The current scene provides local playback, small transfer size and responsive framing.

## Second visual pass

The visual system uses optical white, ink typography and cobalt for selected controls. The glass sculpture provides a broader spectrum of color; a dark workflow band provides contrast. The forum uses neutral reading text, compact navigation and restrained category indicators. There are no invented community discussions or performance figures.

References inspected in a browser, with local reference screenshots under ignored `test-results/references/`:

- https://www.awwwards.com/websites/technology/ : technology selection, composition and typography research. No award claim or copied project assets.
- https://reactbits.dev/ and the repository's `AnimatedContent` / `FadeContent` implementations: entrance timing and lifecycle research. No React Bits component source was copied; animation is implemented with Motion.
- https://motionsites.ai : inspected the public motion/template catalog for scene composition. No paid prompt, template or video was reused.
- https://linear.app : inspected product hierarchy, spacing and functional presentation. No copied source or brand assets.
- https://www.producthunt.com : access returned a Cloudflare challenge; no product or design claims are based on it.

Browser QA covers animated/nonblank WebGL pixels and scene framing at 1440, 1920, 390 and 360 widths, reduced motion, image fallback, model search/filtering, dialogs and account flows. Forum QA uses an isolated database for publishing, reply persistence, text escaping and mobile layouts.

## Data and integration

`/api/site` returns session state, CSRF token and real model shares.
`/api/reference` returns the fixed, source-grounded experiment report.
`/share`, `/login`, `/logout`, `/m/{id}` reuse the existing authenticated workflow.
`/downloads/recipe` and `/downloads/report` serve explicitly allowed files.
No training is started from the public website. The start action opens the local workbench or its setup instructions.

Authors can optionally supply a training recipe, out-of-sample test period, Rank IC and net-of-cost annualized excess return. These fields are stored separately from tool-generated model metadata and labeled as author-reported. Metrics require a test period and finite numeric values. Existing SQLite databases receive an additive, data-preserving research column migration.

## Research forum

`/community` is the dedicated discussion forum. Categories cover model sharing, training methods, performance reviews and questions. `/community/new` provides a full-page composer with session draft storage; `/community/t/{id}` shows a persistent topic, optional linked model and training recipe, and replies. Posting and replying require the existing login and CSRF protection. Login returns to the originating forum page. Topics can link only to the current author's published models; the existing model import form is available at `/?publish=model`.

The public database starts empty. Browser workflow QA uses a temporary isolated database on port 8801, so test topics and replies never enter the real community.

## Run

Install site dependencies using `install.ps1 -WithSite` (Windows) or `sh install.sh --with-site` (Linux).
From `website`: `npm ci`, then `npm run build`.
On Windows use `website/start.ps1` for loopback-only local preview. On Linux set `QWB_SITE_DEV=1` and run `.venv/bin/python -m uvicorn share_site.app:app --host 127.0.0.1 --port 8800` from the root.
Open http://127.0.0.1:8800 .

For frontend development, run the backend on port 8800 and `npm run dev` in `website`; Vite proxies API and sharing requests.

For a public launch configure `QWB_SITE_SECRET`, `HF_OAUTH_CLIENT_ID`, `HF_OAUTH_CLIENT_SECRET`, and `HF_OAUTH_REDIRECT_URI`, then use HTTPS. The local development login is only available from loopback.

The integrated Docker/Compose/Caddy deployment and backup commands are in [deploy/README.md](../deploy/README.md).
