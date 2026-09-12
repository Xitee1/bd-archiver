# Wiki updates for disc folder output

The GitHub Wiki is a separate repository. `disc-folder-output.patch` contains
its documentation changes so they can be reviewed with the code PR before
publishing the new behavior in the live wiki.

Base wiki commit: `fbfcaf59a7ac3d99e10bfc4a80f43eea28d59523`.

After the code change is merged, apply the reviewed patch to a clean, current
wiki checkout (commands run from the main repository):

```bash
git -C ../bd-archiver.wiki apply --check "$PWD/docs/wiki-updates/disc-folder-output.patch"
git -C ../bd-archiver.wiki apply "$PWD/docs/wiki-updates/disc-folder-output.patch"
git -C ../bd-archiver.wiki diff --check
```

Review the result before committing and publishing it in the wiki repository.
If these changes are already present in a local wiki checkout, do not apply them
a second time. If the wiki has changed since the base commit, reconcile the
patch with the newer content before publishing.

The wiki changes have not been published as part of this PR.
