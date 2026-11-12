Review the FINAL content delta of branch hv/ty-mnemos-api-routes-tunnels-20261105
against master base ac84f3f6. Run exactly:

  git diff ac84f3f6 HEAD

That diff is the whole change: it must add exactly two files --
tests/test_mnemos_api_routes_tunnels_characterisation_20261105.py (a
characterisation suite for the tunnel admin routes in
mnemos/api/routes/tunnels.py, which is UNCHANGED by this branch) and the
REVIEW_VERDICT file. Nothing else may differ from ac84f3f6: verify the
branch does NOT modify any mnemos/ source file, does not delete tests, and
does not reintroduce the retired db2 HASH_SHA256/truncating-cast SQL.

Points to check in the suite:
 - every reference to mnemos.api.routes.tunnels is real and current;
 - patches land where the handler actually reads (route module globals);
 - no test depends on external binaries, network, or the real bridge classes;
 - assertions pin behaviour, not line numbers;
 - the credential-before-spawn ordering test cannot pass without the
   ordering it claims.

Answer with a structured JSON verdict. Only approve if you find no defects.
