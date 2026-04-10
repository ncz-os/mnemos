# MNEMOS Repository Layout

Authoritative Git storage and working-copy layout for MNEMOS.

## Authoritative locations

- Bare repository: `/mnt/datapool/git/mnemos-production.git`
- Canonical working tree on ARGONAS: `/mnt/datapool/workspaces/mnemos/main`

## Compatibility path

The legacy bare-repo path below is retained as a symlink for backward compatibility:

- `/mnt/argonas/git/mnemos-production.git` -> `/mnt/datapool/git/mnemos-production.git`

## Operational guidance

- Treat ARGONAS bare repo as the source of truth for Git history.
- Treat the ARGONAS working tree above as the canonical cleanup/release workspace.
- PYTHIA runtime checkout may be ahead or dirty during active development, but changes should be committed and pushed back to the authoritative ARGONAS bare repo.
- Do not create additional bare repos for MNEMOS under alternate paths.

## Historical note

A stale legacy bare repo previously existed at `/mnt/argonas/git/mnemos-production.git`.
It was moved aside during standardization to avoid ambiguity.
