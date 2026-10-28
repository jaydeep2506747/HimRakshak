# Static demo deployment

The public HimRakshak demo is published as a GitHub Pages static site. The
site contains only the self-contained `index.html` dashboard; Python source,
tests, documentation, Wokwi files, caches, and local snapshots remain in the
source repository but are not copied into the Pages artifact.

## Deployment workflow

`.github/workflows/pages.yml` runs on pushes to `main` and on manual workflow
dispatch. It:

1. Checks out the repository.
2. Configures GitHub Pages.
3. Creates a clean `site/` directory containing only `index.html`.
4. Verifies that the entry page exists and that no extra top-level artifact
   files were created.
5. Uploads the directory with `actions/upload-pages-artifact`.
6. Deploys it through the `github-pages` environment with
   `actions/deploy-pages`.

The workflow does not start the Python API and does not publish credentials,
development archives, tests, or caches.

## Expected public URL

For this repository, the expected URL is:

`https://jaydeep2506747.github.io/HimRakshak/`

Treat the URL as usable only after the Pages workflow completes successfully
and the page has been opened and checked. A workflow file or repository
setting alone is not evidence that the site is live.

## Online functionality

The hosted page runs the browser-only simulation. Visitors can:

- Select the three virtual stations.
- Run normal, heavy-rain, slope-shift, and vibration-burst scenarios.
- Adjust simulated sensor controls.
- Import a virtual JSON packet for local validation and reassessment.
- View local history and export the browser session as CSV.

The page visibly identifies itself as a **Static hosted demo · Python API
unavailable** when the local API probe cannot connect. It does not claim that
an API request, packet delivery, or reliability operation succeeded.

## Local API-only functionality

The Python API remains local-only and is not deployed to GitHub Pages.
Communication drop, delay, release, duplicate, stale, disconnect, reconnect,
and other backend reliability operations require:

```powershell
python -m himrakshak.server --port 8765 --seed 42
```

Then open `http://127.0.0.1:8765/`. The public static page cannot provide
backend-backed station history or API delivery guarantees.

## Checking and troubleshooting deployments

From the repository page, open **Actions** and select **Deploy HimRakshak
demo**. The build job must complete before the deploy job can run. A failed
build usually indicates a missing `index.html` or an artifact validation
failure; a failed deploy usually indicates Pages is not enabled for GitHub
Actions or that repository permissions/settings changed.

The GitHub CLI checks are:

```powershell
gh run list --workflow pages.yml --limit 5
gh run view <run-id> --log-failed
gh api repos/jaydeep2506747/HimRakshak/pages
```

If Pages has not been configured, enable **Settings → Pages → Build and
deployment → Source: GitHub Actions**, then rerun the workflow. Do not switch
the source to a branch-based deployment because that would bypass the
artifact validation in this workflow.

## Redeploying after a future change

Commit and push changes to `main`, or use **Run workflow** in the Actions tab
for a manual redeployment. The workflow rebuilds the artifact from the current
`index.html`; it does not maintain a second dashboard copy.

## Source repository versus published artifact

The GitHub repository remains the complete engineering project, including
`himrakshak/`, `tests/`, `docs/`, `wokwi_esp32_demo/`, `README.md`, and
configuration. The GitHub Pages artifact is intentionally limited to:

```text
site/
└── index.html
```
