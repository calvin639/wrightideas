// CloudFront Function — HTTP Basic Auth gate for the dev frontend.
//
// Runs at viewer-request on every request to memories.wrightideas.biz, before
// CloudFront looks at its cache or the S3 origin. Anyone without the password
// gets a 401 and never reaches the site.
//
// WHY A CLOUDFRONT FUNCTION
// It is the only option here that costs effectively nothing and needs no
// infrastructure: no Cognito user pool, no login page, no session store, no
// Lambda@Edge cold starts. Pricing is $0.10 per million invocations, so dev
// traffic rounds to zero. It also runs in all edge locations, so there is no
// unprotected path to the origin.
//
// WHAT THIS IS NOT
// The password lives in this file's deployed source, which anyone with AWS
// console read access can see. That is fine for keeping a work-in-progress off
// the open internet; it is not a secret store and it is not authentication for
// real user data. Before this site handles live customer orders, this gate
// should be removed rather than relied on.
//
// The literal below is replaced at deploy time by scripts/setup-dev-auth.sh —
// the real password is never committed.

function handler(event) {
  var request = event.request;
  var headers = request.headers;

  // "user:pass" base64-encoded. Precomputed because CloudFront Functions have
  // no btoa/Buffer and a very tight CPU budget.
  var EXPECTED = 'Basic __BASIC_AUTH_B64__';

  if (headers.authorization && headers.authorization.value === EXPECTED) {
    return request;
  }

  return {
    statusCode: 401,
    statusDescription: 'Unauthorized',
    headers: {
      // Triggers the browser's native username/password prompt. The realm
      // string is shown in that dialog.
      'www-authenticate': { value: 'Basic realm="Memories in Stone — preview"' },
      // Belt and braces: a 401 is not indexable anyway, but if this function is
      // ever detached the header keeps crawlers off whatever they do reach.
      'x-robots-tag': { value: 'noindex, nofollow, noarchive' },
      'cache-control': { value: 'no-store' }
    }
  };
}
