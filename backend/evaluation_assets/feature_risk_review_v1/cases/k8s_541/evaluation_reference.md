### Risks and Mitigations

#### Client authentication to the binary

Credential provider can authenticate the caller via env vars or arguments
specified in its `kubeconfig`. This is optional.

It is recommended to restrict access to the binary using exec Unix permissions.

#### Invalid credentials before cache expiry

Credentials may become invalid (e.g. expire) after being returned by the
provider but before `expirationTimestamp` in the returned `ExecCredential`.

Credential provider should ensure validity of the credentials it returns and
return an error if it cannot provide valid credentials.

In case the client gets a `401 Unauthorized` response status from the remote
endpoint when using credentials from a provider, the client should re-execute
the provider and disregard the `expirationTimestamp`.

### Test Plan

Unit tests to confirm:

- Version mismatch is detected
- Credentials are cached in-memory correctly
  + Executable is only called as needed
  + Expired credentials are rotated automatically
  + Credentials are used across many requests (as long as they are still valid)
- Single flight all calls to a given executable (when the config is the same)
- Reasonable timeout to executable calls so clients do not hang indefinitely
- `"k8s.io/client-go/pkg/apis/clientauthentication".Cluster` (and external types)
  fields (Go and JSON) properly shadow
  `"k8s.io/client-go/tools/clientcmd/api/v1".Cluster` fields (with the exception of
  `CertificateAuthority` for reasons stated in design) so
  that structs are kept up to date
- Helper methods properly create `"k8s.io/client-go/rest".Config` from
  `"k8s.io/client-go/pkg/apis/clientauthentication".Cluster` and vice versa
- Metrics are reported as they should

Integration (or e2e CLI) tests to confirm:

- Shared informers backed by exec credential work as expected
  + Credential rotation does not cause issues
  + Transient failures are correctly retried
  + Executables requiring interactive prompts fail gracefully
  + Executables are not called in a hot loop during transient failure
- Static forms of auth should interact correctly with exec credential plugin
  + Basic auth
  + Token based auth
  + Cert based auth
- Interactive login flows work
  + TTY forwarding between client and executable works
  + `kubectl` commands and exec credential plugins do not fight for standard input
  + All `InteractiveMode` values are supported
- Metrics are reported as they should

### Graduation Criteria

#### Beta

Feature is already in Beta.

#### Beta -> GA Graduation

- Three examples of real world usage
  + Confirm interactive and non-interactive UX is acceptable
  + Confirm no hacks are being performed to workaround limitations
  + Confirm that configuration of plugin
    * Is correctly handled
    * Is well-supported by the `kubeconfig` file format
- Create the `client.authentication.k8s.io/v1` `ExecCredential` struct
- Address known bugs and add tests to prevent regressions
- Docs are up-to-date with latest version of APIs
- Docs describe set of best practices (i.e. do not mutate `kubeconfig`)
- Sufficient metrics

Note: this feature set does not need conformance tests because it is inherently
opt-in on the client-side and it relies on an extra binary to be present.

### Upgrade / Downgrade Strategy

The distribution of executables to end users for use with clients is out of the
scope of this KEP.  Thus end users are responsible for confirming that the
executable they are attempting to use is compatible with `exec.apiVersion`.

### Version Skew Strategy

The client is aware of its configured `exec.apiVersion`.  It must validate that
the status response from the executable has the matching API version to prevent
it from misinterpreting the response.

## Production Readiness Review Questionnaire

### Feature enablement and rollback

* **How can this feature be enabled / disabled in a live cluster?**
  - [ ] Feature gate (also fill in values in `kep.yaml`)
    - Feature gate name:
    - Components depending on the feature gate:
  - [x] Other
    - Describe the mechanism:
      - This feature is explicitly opt-in since it requires the presence of
        kubeconfig settings.
    - Will enabling / disabling the feature require downtime of the control
      plane?
        - No. Disabling the feature would result in the client needing to choose
          a different authentication method.
    - Will enabling / disabling the feature require downtime or reprovisioning
      of a node? (Do not assume `Dynamic Kubelet Config` feature is enabled).
        - No. Disabling the feature would result in the client needing to choose
          a different authentication method.

* **Does enabling the feature change any default behavior?**
  - No. The feature is explicitly opt-in, so default behavior will be preserved
    unless the client's `kubeconfig` has been updated.

* **Can the feature be disabled once it has been enabled (i.e. can we rollback
  the enablement)?**
  - Yes. Since the feature is explicitly opt-in, disabling the feature can be
    done simply by changing `kubeconfig` settings.

* **What happens if we reenable the feature if it was previously rolled back?**
  - Nothing. The feature will start respecting the explicit opt-in `kubeconfig`
    settings again, just as it would if it was enabled for the first time.

* **Are there any tests for feature enablement/disablement?**
  - There are unit tests in `k8s.io/client-go/plugin/pkg/auth/exec` that
    verify what happens when various parts of this feature set are enabled (e.g.,
    `provideClusterInfo`)
  - There are unit tests in `k8s.io/client-go/tools/clientcmd/...` that validate
    `kubeconfig`'s are handled correctly when they do not contain exec plugin
    configuration.
  - There are unit tests in `k8s.io/client-go/rest` that validate what happens when
    a REST client does not have an exec plugin configuration.

### Rollout, Upgrade and Rollback Planning

* **How can a rollout fail? Can it impact already running workloads?**
  - It is very unlikely that a rollout would fail. If you upgrade your client to
    a version that contains this exec plugin feature set, then your client would still
    continue to function as it did before, since the new behavior that this KEP provides
    is opt-in via a `kubeconfig`.
  - If a client did indeed enable the corresponding settings in its `kubeconfig` after
    rolling out this feature, then it may cause a client-side authentication failure if
    the client's exec plugin fails to return a credential properly. However, this would be
    an issue on the client side with a third-party exec plugin.

* **What specific metrics should inform a rollback?**
  - Note that `kubectl` isn't the only consumer of client-go that can make use of these
    exec plugins. Some client-go consumers are long-running and publish metrics that could
    give visibility to the health of the exec plugin and surrounding machinery.
  - When a certificate credential is refreshed (i.e., upon the first invocation of an exec
    plugin within a client's runtime, when the credential has expired, or when we get a
    401 HTTP status from the API), the certificate's expiration time will be emitted as a
    metric. The certificate expiration should remain constant until the expiration time
    when it should get increased. If this is not the case, then the exec plugin
    authenticator could be behaving incorrectly. For example, if the certificate
    expiration time is constantly increasing upon every authentication to the API, then
    perhaps the exec plugin authenticator is refreshing the certificate credential too
    often. Furthermore, the certificate's age (i.e., the time since the certificate's
    `NotBefore` field) will be emitted as a metric. If this value is frequently much smaller
    than the certificate's expected lifetime, then the exec plugin authenticator may be
    rotating credentials too quickly which may point to a bug.
  - The total number of calls to the exec plugin would also be helpful to obtain.  This
    metric should increase each time a credential is refreshed (see previous bullet point
    for when this happens). If this number increases rapidly, then the exec plugin
    authenticator could be behaving incorrectly. For example, the exec plugin could be
    receiving 401 HTTP statuses from the API, or the calculation of the expiration time
    could be incorrect, or the credential could have been incorrectly evicted from the
    exec plugin authenticator's cache.
  - The number of errors encountered when calling the exec plugin would also be helpful to
    obtain. This metric should ideally remain very low. If this number increases very
    quickly, then then one may want to inspect why the client is not able to run the exec
    plugin by viewing the client's logs or running the exec plugin manually in the target
    environment.

* **Were upgrade and rollback tested? Was upgrade->downgrade->upgrade path tested?**
  - N/A.

* **Is the rollout accompanied by any deprecations and/or removals of features,
  APIs, fields of API types, flags, etc.?**
  - Deprecation of `gcp` and `azure` authentication options. These authentication options
    can be used going forward via this exec plugin feature set.
  - Otherwise, this feature set contains the usual alpha, beta, and GA
    stages, and will follow the same canonical deprecation pattern for
    its API versions.

### Monitoring requirements

_This section must be completed when targeting beta graduation to a release._

* **How can an operator determine if the feature is in use by workloads?**
  - Clients provide metrics for usage today.
  - One could also look in the `kubeconfig` in use by the client to see if an exec
    credential provider is being used.

* **What are the SLIs (Service Level Indicators) an operator can use to
  determine the health of the service?**
  - [X] Metrics
    - Metric name: `rest_client_exec_plugin_ttl_seconds`, `rest_client_exec_plugin_certificate_rotation_age`,
      `rest_client_exec_plugin_call_total`
    - Components exposing the metric: client-go
  - [ ] Other (treat as last resort)
    - Details:
      - This feature set operates on the client-side.

* **What are the reasonable SLOs (Service Level Objectives) for the above SLIs?**
  - We target certificate rotations to happen within 1% of a certificate's
    lifetime. This is measured by
    `rest_client_exec_plugin_certificate_rotation_age` and
    `rest_client_exec_plugin_ttl_seconds`.
  - We target 0.01% unsuccessful calls to the exec plugin in a moving 24h
    window. This is measured by
    `rest_client_exec_plugin_call_total`.

* **Are there any missing metrics that would be useful to have to improve
  observability if this feature?**
  - As discussed [above](#rollout-upgrade-and-rollback-planning), the total number of
    calls and number of errors encountered when calling the exec plugin would make the
    behavior of this feature set more observable.

### Dependencies

* **Does this feature depend on any specific services running in the cluster?**
  - No.

## Implementation History

- 2018-01-29: Proposal submitted https://github.com/kubernetes/community/pull/1503
- 2018-02-28: Alpha (v1.10) implemented https://github.com/kubernetes/kubernetes/pull/59495
- 2018-06-04: Promoted to Beta (v1.11) https://github.com/kubernetes/kubernetes/pull/64482
- 2019-11-22: `rest_client_exec_plugin_ttl_seconds` and `rest_client_exec_plugin_certificate_rotation_age` metrics added (v1.18) https://github.com/kubernetes/kubernetes/pull/84382
- 2020-07-09: `InstallHint` added to Beta API (v1.19) https://github.com/kubernetes/kubernetes/pull/91305
- 2020-10-29: `ProvideClusterInfo` added to Beta API (v1.20) https://github.com/kubernetes/kubernetes/pull/95489
- 2021-03-04: `rest_client_exec_plugin_call_total` metric added (v1.21) https://github.com/kubernetes/kubernetes/pull/98892
- 2021-06-15: `InteractiveMode` added to Beta API (v1.22) https://github.com/kubernetes/kubernetes/pull/99310
- 2021-05-11: Stable API approved (v1.22) https://github.com/kubernetes/enhancements/pull/2587
- 2021-07-06: Promoted to Stable (v1.22) https://github.com/kubernetes/kubernetes/pull/102890
