### Risks and Mitigations

<!--
What are the risks of this proposal, and how do we mitigate? Think broadly.
For example, consider both security and how this will impact the larger
Kubernetes ecosystem.

How will security be reviewed, and by whom?

How will UX be reviewed, and by whom?

Consider including folks who also work outside the SIG or subproject.
-->
The following is the list of hypothetical scenarios how users may decide to
abuse the feature or use it in a non-designed way. This is an exercise to
understand potential side effects from implementing the feature. We are looking
for side effects like abuse of the functionality to implement something
error-prone that sidecars were not designed for. Or causing issues like noisy
neighbors, etc.

#### Scenario 1. User decides to use sidecars as a way to run regular containers

At least one regular container is required by the Pod. Users may decide to run
all other containers as sidecars and have a single container with the Sleep
logic inside with timeout or waiting for some cancellation signal from custom
job orchestration portal. This way users may implement execution with a time
limit defined by that logic. This is something that users cannot express today
“declaratively” and will be possible using new sidecar containers. One may
imagine a scenario when a third party jobs orchestration tool converts all
containers to sidecar containers programmatically and injects a single job
orchestration container into the Pod. This can be seen as an abuse of the
concept, and may lead to issues when pods terminate unexpectedly or Jobs meant
to be run as regular containers start being run as sidecars being restarted on
Completion. However we do not see a major problem with it as this use of sidecar
containers will unlikely lead to "unexpected" behavior - all side effects are
quite clear.

Another reason for users to implement regular containers as sidecars would be to
use any special properties kubelet may apply to sidecar containers. For example,
if restart backoff timeouts will be minimized for sidecar containers (which was
proposed and rejected), users may decide to use this feature for regular
containers by running those as sidecars. Current proposal as written doesn’t
introduce any special properties for sidecars that users may start abusing.
Special OOM score adjustment will unlikely be useful as this kind of abuse will
likely be needed for bigger containers that does not have the problem of OOM
score adjustment to be too low.

#### Scenario 1.a

User decides to run regular container as a sidecar to simply guarantee the
startup sequence of the containers. Users can already implement the startup
sequencing by using blocking nature of `postStart` hook. So using the sidecar
containers instead is not adding any benefits.

As for injecting sidecars into Pods with regular containers run as sidecars -
webhooks will likely inject sidecar containers to be first, so the risk is
minimal here as well.

#### Scenario 2. Balloon sidecars

Users may decide to start the “large request” sidecar containers early to
pre-allocate resources for other containers. The same time asking for less
resources for a regular containers hoping to reuse what is allocated by the
“balloon” sidecar. This was previously impossible to pre-allocate resources like
CPU before any init containers run and may be more critical when resource
managers like CPU or topology managers are being used. If this pattern have any
benefits, people may be incentivized to abuse sidecars this way. However it’s
unclear if this has any benefits for users at all. 

#### Scenario 3. Long initialization tasks running in-parallel

Users may decide to implement long initialization tasks that will run
in-parallel with other initialization tasks. Users may also decide to run new
type of initialization tasks  like image preloading for workload containers from
the sidecar container, which will make it run in parallel with the
Initialization tasks. This is impossible to do today as only a single Init
container is run at a time today. For containers with lengthy initialization
this pattern may be abused and can lead to the pattern of sidecars
synchronization when the first workload container will wait for all Init sidecar
containers to complete. This pattern may lead to race conditions and be error
prone.

Today similar behavior can be achieved by running initialization tasks as
regular containers, with the special container that blocks workload from
execution using synchronization logic in the `PostStart` handler. Sidecars support
makes implementation of this feature easier.

There is not much risk, however, even if the user abuses this pattern by
converting all init containers in sidecars. Whenever this pattern is useful, the
user will most likely need to spend time understanding the logic of different
Init Containers and making sure it is not conflicting. This pattern also likely
prohibited for large scale customers because sidecar-based initialization will
cost resources even after initialization is complete.

#### Scenario 4. Sidecar that never becomes ready 

This is the failure mode when the sidecar never becomes ready. This is
functionally equivalent to the init container that never completes and not
allowing users to implement any other ways to abuse kubelet.

#### Scenario 5. Intentional failing or terminating sidecars

Users may implement a sidecar container that intentionally crashes or terminates
often. This scenario functionally similar to a regular container restarts often
so not much additional overhead or side effects will be created. 

#### Scenario 6. Keeping a sidecar alive to keep consuming cycles on termination

On a multitenant cluster, you may have time limits placed on jobs to enable fair
usage across tenants. If a sidecar can prevent termination indefinitely, it
could be used to perform computation outside the allowed limit.

#### Scenario 7. Risk of porting existing sidecars to the new mechanism naively

There is risk associated with people moving sidecars as implemented today to use
the new pattern. We didn’t receive any feedback on potential downsides. One
scenarios that may be affected is if sidecars decided to terminate itself and
kubelet keeps trying to restart it as one of the main containers are still being
terminated. Based on current patterns for sidecar containers, this is not likely
the problem. 

Another potential problem may be that sidecars will wait for some condition to
mark itself “started” that cannot be met with the new pattern. For example, wait
for other containers to fully start. As sidecar will not be marked as startup
completed, other init containers will not run and Pod will be stuck on
initialization. For example, Knative's sidecar does aggressive probing of the
user's container to ensure they're ready prior to marking the sidecar ready
itself. This prevents the Pod from being included K8s ready endpoints. See the
section ["Pod startup completed condition"](#pod-startup-completed-condition)
for more details.

Switching to the new sidecars approach will slow down Pod start for Istio. Istio
today is not blocking other containers to start during its initialization. With
the switch to the new model, the separation of Initialization stage and main
containers running stage will be more explicit and many implementations will
likely wait for sidecar initialization, effectively slowing down Pod startup
comparing to current approach. This can be eliminated by slight redesign of a
sidecars.

#### Resource calculation and version skew

In case of a version skew between scheduler and kubelet, or in cases when
scheduler and kubelet has a different value set for the `SidecarContainers` feature gate,
calculation of resources required for a Pod will differ between the scheduler
and a kubelet when the sidecar container created.

In case when scheduler "knows" about the sidecar and kubelet doesn't, there
unlikely be any issues. Scheduler will calculate resources usage for a Pod that
will be equal or more than kubelet will require to run the Pod. So there will be
no overbooking.

If scheduler has the `SidecarContainers` feature gate disabled, the Pod that has a Sidecar
container will not be admitted as validation of the new field will fail.

We will recommend in documentation to not disable the feature gate on scheduler,
while there are any Pods with Sidecar container is running.

### Test Plan

<!--
**Note:** *Not required until targeted at a release.*
The goal is to ensure that we don't accept enhancements with inadequate testing.

All code is expected to have adequate tests (eventually with coverage
expectations). Please adhere to the [Kubernetes testing guidelines][testing-guidelines]
when drafting this test plan.

[testing-guidelines]: https://git.k8s.io/community/contributors/devel/sig-testing/testing.md
-->

[ ] I/we understand the owners of the involved components may require updates to
existing tests to make this code solid enough prior to committing the changes necessary
to implement this enhancement.

##### Prerequisite testing updates

<!--
Based on reviewers feedback describe what additional tests need to be added prior
implementing this enhancement to ensure the enhancements have also solid foundations.
-->

##### Unit tests

<!--
In principle every added code should have complete unit test coverage, so providing
the exact set of tests will not bring additional value.
However, if complete unit test coverage is not possible, explain the reason of it
together with explanation why this is acceptable.
-->

<!--
Additionally, for Alpha try to enumerate the core package you will be touching
to implement this enhancement and provide the current unit coverage for those
in the form of:
- <package>: <date> - <current test coverage>
The data can be easily read from:
https://testgrid.k8s.io/sig-testing-canaries#ci-kubernetes-coverage-unit

This can inform certain test coverage improvements that we want to do before
extending the production code to implement this enhancement.
-->

- `<package>`: `<date>` - `<test coverage>`

There will be many packages touched in process. A few that easy to identify by
areas of a change:

Admission:
- `k8s.io/kubernetes/pkg/kubelet/lifecycle`: `61.7`

Enable probes for sidecar containers
- `k8s.io/kubernetes/pkg/kubelet/prober`: `02/07/2023` - `79.9`

Include sidecars into QoS decision:
- `k8s.io/kubernetes/pkg/kubelet/qos`: `02/07/2023` - `100`

Include sidecar in resources calculation and policy decisions:

- `k8s.io/kubernetes/pkg/kubelet/cm/topologymanager`: `02/07/2023` - `93.2`
- `k8s.io/kubernetes/pkg/kubelet/cm/memorymanager`: `02/07/2023` - `81.2`
- `k8s.io/kubernetes/pkg/kubelet/cm/cpumanager`: `02/07/2023` - `86.4`

Update OOM score adjustment:
-  `k8s.io/kubernetes/pkg/kubelet/oom`: `02/07/2023` - ` 57.1`

##### Integration tests

<!--
Integration tests are contained in k8s.io/kubernetes/test/integration.
Integration tests allow control of the configuration parameters used to start the binaries under test.
This is different from e2e tests which do not allow configuration of parameters.
Doing this allows testing non-default options and multiple different and potentially conflicting command line options.
-->

<!--
This question should be filled when targeting a release.
For Alpha, describe what tests will be added to ensure proper quality of the enhancement.

For Beta and GA, add links to added tests together with links to k8s-triage for those tests:
https://storage.googleapis.com/k8s-triage/index.html
-->

No integration tests are planned. We'll cover this with e2e_node tests.

##### e2e tests

<!--
This question should be filled when targeting a release.
For Alpha, describe what tests will be added to ensure proper quality of the enhancement.

For Beta and GA, add links to added tests together with links to k8s-triage for those tests:
https://storage.googleapis.com/k8s-triage/index.html

We expect no non-infra related flakes in the last month as a GA graduation criteria.
-->

- Test failures: https://storage.googleapis.com/k8s-triage/index.html?pr=1&test=SidecarContainers
- All related tests can be filtered with the SidecarContainers

##### Ready state of a sidecar container is properly used to create/delete endpoints

The sidecar container can expose ports and can be used to handle external traffic.

- Pod with sidecar container exposing the port can receive Service traffic to this Port
- Pod with sidecar container exposing the port with readiness probe marks the Endpoint not ready when probe fails and switched back on when readiness probe succeed
- Pod with sidecar container exposing the port can receive Service traffic to this Port during Pod termination (during graceful termination period)

##### Pod lifecycle scenarios without sidecar containers

- Init containers should start after the previous init container has completed
- Regular containers should start in parallel after all init containers have completed
- Restart behavior of the init containers

##### Pod lifecycle scenarios with sidecar containers

- Init containers should start after the previous restartable init container has started
- Regular containers should start in parallel after all regular init containers have completed and all restartable init containers have started
- Restartable init containers should restart always

##### Kubelet restart test cases

- It should restart the containers in the right order after the node reboot
- It should not restart any completed init containers after the kubelet restart

##### API server is down: failure to update containers status during initialization

From @rata. Interesting test cases where the API server is down and kubelet
wants to start a pod with sidecars. That was problematic in the previous
sidecar KEP iterations.

Let say the API server is up and a pod is being started by the kubelet
- The kubelet still needs to start 3 sidecars in the pod
- The API server crashes (it is not restarted yet)
- The kubelet continues to try to start the sidecars
- The pod is started correctly
- The API server becomes reachable again

I think testing this scenario works is important in early phases (alpha), as in
the past that proved to be tricky. The kubelet is authoritative on which
containers are started and sends it to the API server, but I hit some bugs in
the past where if the API server was down, we couldn't read that a new container
was ready from the kubelet. The code to do it was there even if API server was
down, but something was not working and I didn't debug it. And the end result
was that the pod startup didn't finish until the API server was up again, as
that is when we realized from the kubelet that the sidecar was ready.

I think this code has changed since 1.17 when I tested this, but I don't know if
this issue is fixed. It is a non-trivial scenario that, if it happens to need
more serious code changes in the kubelet to handle it correctly, it will be good
to know in early stages of the KEP IMHO.

#### Resource usage testing

1. Validate that the pod overhead will be accounted for when scheduling a Pod
   with sidecar container.
2. Validate that LimitRanger will apply defaults and consider limits for the Pod
   with the sidecar containers.

#### Upgrade/downgrade testing

1. Kubelet and control plane reject the Pod with the new field if the feature
  gate is disabled.
2. kubelet and control plane reject the Pod with the new field if the feature
  gate was disabled AFTER the Pod with the new field was added.

### Graduation Criteria

#### Alpha

- Feature implemented behind a feature flag
- Initial e2e tests completed and enabled
- E2e testing of the existing scenarios with and without the feature gate turned
  on

#### Beta

- Implement proper termination ordering.
- Add tests with feature activation and deactivation (see [Feature Enablement and Rollback](#feature-enablement-and-rollback)).

#### GA

- All known issues are fixed
- Production use feedback addressed

### Upgrade / Downgrade Strategy

#### Upgrade strategy

Existing sidecars (implemented as regular containers) will still work as
intended, as in the past we don't recognize the new field `restartPolicy` today.

Upgrade will not change any current behaviors.

#### Downgrade strategy

First, there will be no effect on any workload that doesn't use a new field. Any
combination of feature gate enabled/disabled or version skew will work as usual
for that workload.

When the new functionality wasn't yet used, downgrade will not be affected.

Versions of Kubernetes that doesn't have this feature implemented will ignore
and strip out the new field `initContainers`. 

Pods that has already been created will stay being scheduled after the downgrade - 
not be rejected by control
plane nor by kubelet. Both will treat the sidecar container as an Init container.
This may render the Pod unusable as it will stuck in initialization forever -
sidecar container are never exiting.
This behavior has been documented for Alpha release, but we don't see it as a
major issue requiring to wait for 3 releases so kubelet will have the logic
to reject such Pods when the feature gate is disabled to keep Downgrade safe.

**Note**, We have implemented logic for the
[kubelet](https://github.com/kubernetes/kubernetes/blob/f19b62fc0914b38941922afefd1e34eb55f87ee7/pkg/kubelet/lifecycle/predicate.go#L78-L91)
to reject Pods with sidecar containers when feature gate is turned off.
For the control plane -
[kube-apiserver](https://github.com/kubernetes/kubernetes/blob/f19b62fc0914b38941922afefd1e34eb55f87ee7/pkg/api/pod/util.go#L554-L560)
is dropping the field (if it wasn't set before) and
[kube-scheduler](https://github.com/kubernetes/kubernetes/blob/f19b62fc0914b38941922afefd1e34eb55f87ee7/pkg/scheduler/framework/plugins/noderesources/fit.go#L256-L262)
is keeping pods with the field set unschedulable.
See [Upgrade/downgrade testing](#upgradedowngrade-testing) section.

Workloads will have to be deleted and recreated with the old way of handling
sidecars.  Once there is no more Pods using sidecars, node can be downgraded
without side effects.

If downgrade happening from the version with the feature enabled to the previous
version that has this feature support, but feature gate is disabled, kubelet
and control place will reject these Pods.

**Note**, downgrade requires node drain. So we will not support scenarios when
Pod already running on the node will need to be handled by the restarted
kubelet that doesn't know about the sidecar containers.

### Version Skew Strategy

<!--
If applicable, how will the component handle version skew with other
components? What are the guarantees? Make sure this is in the test plan.

Consider the following in developing a version skew strategy for this
enhancement:
- Does this enhancement involve coordinating behavior in the control plane and nodes?
- How does an n-3 kubelet or kube-proxy without this feature available behave when this feature is used?
- How does an n-1 kube-controller-manager or kube-scheduler without this feature available behave when this feature is used?
- Will any other components on the node change? For example, changes to CSI,
  CRI or CNI may require updating that component before the kubelet.
-->
Version skew is possible between the control plane and worker nodes as both
should be aware of the new field used to flag sidecars inside `initContainers`.

Therefore all cluster nodes, including control plane nodes, must be upgraded
before the user can deploy sidecars using the new syntax.

Also, since the feature flag applies to both kubelet and the control plane,
similarly all cluster nodes need to have it enabled before deploying Pods with
sidecars.

For the scenarios when the feature gate is disabled on control plane, but not
disabled on kubelet, users will not be able to schedule Pods with the new field.

For the scenarios when the feature gate is disabled on kubelet, but enabled on
control plane, users will be able to create these Pods, but kubelet will reject
those.

## Production Readiness Review Questionnaire

<!--

Production readiness reviews are intended to ensure that features merging into
Kubernetes are observable, scalable and supportable; can be safely operated in
production environments, and can be disabled or rolled back in the event they
cause increased failures in production. See more in the PRR KEP at
https://git.k8s.io/enhancements/keps/sig-architecture/1194-prod-readiness.

The production readiness review questionnaire must be completed and approved
for the KEP to move to `implementable` status and be included in the release.

In some cases, the questions below should also have answers in `kep.yaml`. This
is to enable automation to verify the presence of the review, and to reduce review
burden and latency.

The KEP must have a approver from the
[`prod-readiness-approvers`](http://git.k8s.io/enhancements/OWNERS_ALIASES)
team. Please reach out on the
[#prod-readiness](https://kubernetes.slack.com/archives/CPNHUMN74) channel if
you need any help or guidance.
-->

### Feature Enablement and Rollback

<!--
This section must be completed when targeting alpha to a release.
-->

###### How can this feature be enabled / disabled in a live cluster?

<!--
Pick one of these and delete the rest.

Documentation is available on [feature gate lifecycle] and expectations, as
well as the [existing list] of feature gates.

[feature gate lifecycle]: https://git.k8s.io/community/contributors/devel/sig-architecture/feature-gates.md
[existing list]: https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates/
-->

- [X] Feature gate (also fill in values in `kep.yaml`)
  - Feature gate name: `SidecarContainers`
  - Components depending on the feature gate:
    - kubelet
    - kube-apiserver
    - kube-controller-manager
    - kube-scheduler

###### Does enabling the feature change any default behavior?

<!--
Any change of default behavior may be surprising to users or break existing
automations, so be extremely careful here.
-->

No.

###### Can the feature be disabled once it has been enabled (i.e. can we roll back the enablement)?

<!--
Describe the consequences on existing workloads (e.g., if this is a runtime
feature, can it break the existing applications?).

Feature gates are typically disabled by setting the flag to `false` and
restarting the component. No other changes should be necessary to disable the
feature.

NOTE: Also set `disable-supported` to `true` or `false` in `kep.yaml`.
-->

Yes. Pods that had sidecars will need to be deleted and recreated without them.

The feature gate disablement will require the kubelet restart. When kubelet will
start, it will fail to reconcile the Pod with the new fields and will clean up
all running containers.

If version downgrade is involved, the node must be drained. All Pods with the
new field will not be accepted by kubelet once feature was disabled.

###### What happens if we reenable the feature if it was previously rolled back?

If Pods were in Pending State rejected by kubelet due to "unknown" field to be
scheduled, they may become scheduleable again and will work as expected.

###### Are there any tests for feature enablement/disablement?

<!--
The e2e framework does not currently support enabling or disabling feature
gates. However, unit tests in each component dealing with managing data, created
with and without the feature, are necessary. At the very least, think about
conversion tests if API types are being modified.

Additionally, for features that are introducing a new API field, unit tests that
are exercising the `switch` of feature gate itself (what happens if I disable a
feature gate after having objects written with the new field) are also critical.
You can take a look at one potential example of such test in:
https://github.com/kubernetes/kubernetes/pull/97058/files#diff-7826f7adbc1996a05ab52e3f5f02429e94b68ce6bce0dc534d1be636154fded3R246-R282
-->

See https://github.com/kubernetes/kubernetes/pull/129731/ introducing this test with the emulated version.

### Rollout, Upgrade and Rollback Planning

<!--
This section must be completed when targeting beta to a release.
-->

###### How can a rollout or rollback fail? Can it impact already running workloads?

<!--
Try to be as paranoid as possible - e.g., what if some components will restart
mid-rollout?

Be sure to consider highly-available clusters, where, for example,
feature flags will be enabled on some API servers and not others during the
rollout. Similarly, consider large clusters and how enablement/disablement
will rollout across nodes.
-->

Rollout could fail for multiple reasons:

- webhooks that are not recompiled with the new field will strip it out
- bug in the resource calculation or CPU reservation logic could render the Pod unschedulable
- bug in the kubelet affecting the pod lifecycle could cause the Pod to be stuck in initialization

However, we have tried to maintain a high coverage of unit tests to ensure we catch these.

Rollback can fail if a Pod with sidecars is scheduled on a node where the feature
is disabled.
In that case the Pod will be rejected by kubelet and will be stuck in Pending state.
Therefore, we advise to first disable the feature gate on the control plane and then
proceed with the nodes.

Running workloads are not impacted.

Pods with sidecars might take a long time to exit and exceed the TGPS, a new
event should be added in beta to help administrators diagnose this issue.
Rather than rolling back the feature, they should work on the graceful termination
of their main containers to ensure sidecars have enough time to be notified
and exit on their own.

###### What specific metrics should inform a rollback?

<!--
What signals should users be paying attention to when the feature is young
that might indicate a serious problem?
-->

- [X] Metrics
  - Metric name: kubelet_started_containers_errors_total
    - Type: Counter
    - Labels:code, container_type (should be `init_container`)
    - Components exposing the metric: `kubelet-metrics`
    - Symptoms: high number of errors indicates that the kubelet is unable to start the sidecar containers
- [X] API objects
  - Pods stuck in Pending state of Init container running.
    - Type: API objects
    - Symptoms: when the new field `restartPolicy:Always` was mistakenly stripped out by a webhook, Pod will get stuck.

###### Were upgrade and rollback tested? Was the upgrade->downgrade->upgrade path tested?

<!--
Describe manual testing that was done and the outcomes.
Longer term, we may want to require automated upgrade/rollback tests, but we
are missing a bunch of machinery and tooling and can't do that now.
-->

Upgrade->downgrade->upgrade testing was done manually using the following steps:

Kubelet specific:
1. Deploy k8s 1.29-alpha
2. Enable the `SidecarContainers` feature gate on the control plane and kubelet
3. Deploy a Pod with sidecar containers using a Deployment
4. Disable the `SidecarContainers` feature gate on the kubelet (requires a restart)
5. Drain the node
6. Pod is rejected by kubelet
7. Enable the `SidecarContainers` feature gate on the kubelet (requires a restart)
8. Pod is scheduled and works as expected

Control plane specific:
1. Deploy k8s 1.29-alpha
2. Enable the `SidecarContainers` feature gate on the control plane and kubelet
3. Deploy a Pod with sidecar containers using a Deployment
4. Disable the `SidecarContainers` feature gate on the control plane
5. Delete the Pod
6. Pod is created without the new field - init containers are not recognized as sidecars and block the Pod in initialization
7. Modify the Deployment by moving the sidecar containers to the regular containers section
8. Pod is scheduled and works (without the sidecar support)
9. Enable the `SidecarContainers` feature gate on the control plane
10. Delete the Pod
11. Pod is scheduled and works (without the sidecar support)
12. Modify the Deployment by moving the sidecar containers to the init containers section
13. Pod is scheduled and works (with the sidecar support)

###### Is the rollout accompanied by any deprecations and/or removals of features, APIs, fields of API types, flags, etc.?

<!--
Even if applying deprecation policies, they may still surprise some users.
-->

No.

### Monitoring Requirements

<!--
This section must be completed when targeting beta to a release.

For GA, this section is required: approvers should be able to confirm the
previous answers based on experience in the field.
-->

###### How can an operator determine if the feature is in use by workloads?

<!--
Ideally, this should be a metric. Operations against the Kubernetes API (e.g.,
checking if there are objects with field X set) may be a last resort. Avoid
logs or events for this purpose.
-->

By checking if `.spec.initContainers[i].restartPolicy` is set to `OnFailure` or `Always`.

###### How can someone using this feature know that it is working for their instance?

<!--
For instance, if this is a pod-related feature, it should be possible to determine if the feature is functioning properly
for each individual pod.
Pick one more of these and delete the rest.
Please describe all items visible to end users below with sufficient detail so that they can verify correct enablement
and operation of this feature.
Recall that end users cannot usually observe component logs or access metrics.
-->

End users can check components that are using the new feature, such as Istio, if istio-proxy runs as a sidecar container:

```
$ kubectl get pod -o "custom-columns="\
"NAME:.metadata.name,"\
"INIT:.spec.initContainers[*].name,"\
"CONTAINERS:.spec.containers[*].name"

NAME                     INIT                     CONTAINERS
sleep-7656cf8794-8fhdk   istio-init,istio-proxy   sleep
```

###### What are the reasonable SLOs (Service Level Objectives) for the enhancement?

<!--
This is your opportunity to define what "normal" quality of service looks like
for a feature.

It's impossible to provide comprehensive guidance, but at the very
high level (needs more precise definitions) those may be things like:
  - per-day percentage of API calls finishing with 5XX errors <= 1%
  - 99% percentile over day of absolute value from (job creation time minus expected
    job creation time) for cron job <= 10%
  - 99.9% of /health requests per day finish with 200 code

These goals will help you determine what you need to measure (SLIs) in the next
question.
-->

- number of running containers should not change by more than 10% throughout the day,
  as measured by the number of running containers at the beginning and end of the day
- error rate for containers of type init_container should be less than 1%,
  as measured by the number of errors divided by the total number of init_container containers
- number of events indicating that TGPS has been exceeded should be less than 10 per day,
  as measured by the number of events logged in the kubelet log
- 99% of jobs with sidecars should complete successfully,
  as measured by the number of jobs that complete successfully divided by the total number of jobs with sidecars

###### What are the SLIs (Service Level Indicators) an operator can use to determine the health of the service?

<!--
Pick one more of these and delete the rest.
-->

- [X] Metrics
  - Metric name: kubelet_running_containers
    - Type: Gauge 
    - Labels:container_state
    - Components exposing the metric: `kubelet-metrics`
  - Metric name: kubelet_started_containers_errors_total
    - Type: Counter 
    - Labels:code, container_type (should be `init_container`)
    - Components exposing the metric: `kubelet-metrics`

###### Are there any missing metrics that would be useful to have to improve observability of this feature?

<!--
Describe the metrics themselves and the reasons why they weren't added (e.g., cost,
implementation difficulties, etc.).
-->

No.

### Dependencies

<!--
This section must be completed when targeting beta to a release.
-->

###### Does this feature depend on any specific services running in the cluster?

<!--
Think about both cluster-level services (e.g. metrics-server) as well
as node-level agents (e.g. specific version of CRI). Focus on external or
optional services that are needed. For example, if this feature depends on
a cloud provider API, or upon an external software-defined storage or network
control plane.

For each of these, fill in the following—thinking about running existing user workloads
and creating new ones, as well as about cluster-level services (e.g. DNS):
  - [Dependency name]
    - Usage description:
      - Impact of its outage on the feature:
      - Impact of its degraded performance or high-error rates on the feature:
-->

No.

### Scalability

<!--
For alpha, this section is encouraged: reviewers should consider these questions
and attempt to answer them.

For beta, this section is required: reviewers must answer these questions.

For GA, this section is required: approvers should be able to confirm the
previous answers based on experience in the field.
-->

###### Will enabling / using this feature result in any new API calls?

<!--
Describe them, providing:
  - API call type (e.g. PATCH pods)
  - estimated throughput
  - originating component(s) (e.g. Kubelet, Feature-X-controller)
Focusing mostly on:
  - components listing and/or watching resources they didn't before
  - API calls that may be triggered by changes of some Kubernetes resources
    (e.g. update of object X triggers new updates of object Y)
  - periodic API calls to reconcile state (e.g. periodic fetching state,
    heartbeats, leader election, etc.)
-->

No.

###### Will enabling / using this feature result in introducing new API types?

<!--
Describe them, providing:
  - API type
  - Supported number of objects per cluster
  - Supported number of objects per namespace (for namespace-scoped objects)
-->

No.

###### Will enabling / using this feature result in any new calls to the cloud provider?

<!--
Describe them, providing:
  - Which API(s):
  - Estimated increase:
-->

No.

###### Will enabling / using this feature result in increasing size or count of the existing API objects?

<!--
Describe them, providing:
  - API type(s):
  - Estimated increase in size: (e.g., new annotation of size 32B)
  - Estimated amount of new objects: (e.g., new Object X for every existing Pod)
-->

No.

###### Will enabling / using this feature result in increasing time taken by any operations covered by existing SLIs/SLOs?

<!--
Look at the [existing SLIs/SLOs].

Think about adding additional work or introducing new steps in between
(e.g. need to do X to start a container), etc. Please describe the details.

[existing SLIs/SLOs]: https://git.k8s.io/community/sig-scalability/slos/slos.md#kubernetes-slisslos
-->

Graceful Pod termination might take longer with sidecars since their exit sequence starts after the
last main container has stopped.
The impact should be negligible because the TGPS is enforced in all cases.

###### Will enabling / using this feature result in non-negligible increase of resource usage (CPU, RAM, disk, IO, ...) in any components?

<!--
Things to keep in mind include: additional in-memory state, additional
non-trivial computations, excessive access to disks (including increased log
volume), significant amount of data sent and/or received over network, etc.
This through this both in small and large cases, again with respect to the
[supported limits].

[supported limits]: https://git.k8s.io/community//sig-scalability/configs-and-limits/thresholds.md
-->

No.

###### Can enabling / using this feature result in resource exhaustion of some node resources (PIDs, sockets, inodes, etc.)?

<!--
Focus not just on happy cases, but primarily on more pathological cases
(e.g. probes taking a minute instead of milliseconds, failed pods consuming resources, etc.).
If any of the resources can be exhausted, how this is mitigated with the existing limits
(e.g. pods per node) or new limits added by this KEP?

Are there any tests that were run/should be run to understand performance characteristics better
and validate the declared limits?
-->

No, since the KEP only enable a new way to run containers as sidecars instead of regular containers.
Resource consumption can even be lower since various tricks using emptyDir volumes to perform synchronization
(as with istio-proxy) are no longer needed.

### Troubleshooting

<!--
This section must be completed when targeting beta to a release.

For GA, this section is required: approvers should be able to confirm the
previous answers based on experience in the field.

The Troubleshooting section currently serves the `Playbook` role. We may consider
splitting it into a dedicated `Playbook` document (potentially with some monitoring
details). For now, we leave it here.
-->

###### How does this feature react if the API server and/or etcd is unavailable?

Nothing changes compared to the current kubelet behavior.

###### What are other known failure modes?

<!--
For each of them, fill in the following information by copying the below template:
  - [Failure mode brief description]
    - Detection: How can it be detected via metrics? Stated another way:
      how can an operator troubleshoot without logging into a master or worker node?
    - Mitigations: What can be done to stop the bleeding, especially for already
      running user workloads?
    - Diagnostics: What are the useful log messages and their required logging
      levels that could help debug the issue?
      Not required until feature graduated to beta.
    - Testing: Are there any tests for failure mode? If not, describe why.
-->

- Main containers don't exit within TGPS, leading to sidecars being terminated
  - Detection: high number of events indicating TGPS has been exceeded
  - Mitigations: ensure timely termination of main containers
  - Diagnostics: Events
  - Testing: https://github.com/kubernetes/kubernetes/blob/b4f902f0371485505ff4eda39975e67bfa9b0727/test/e2e_node/container_lifecycle_test.go#L4977-L5077
- Main container or sidecar use a preStop hook consuming TGPS, leading to remaining sidecars being terminated
  - Detection: high number of events indicating TGPS has been exceeded
  - Mitigations: ensure preStop hooks are not delaying termination
  - Diagnostics: Events
  - Testing: https://github.com/kubernetes/kubernetes/blob/b4f902f0371485505ff4eda39975e67bfa9b0727/test/e2e_node/container_lifecycle_test.go#L4272-L4408
- Sidecar container uses a preStop hook that make the container exit during Pod shutdown, sidecar is restarted, leading
to a CrashLoopBackOff
  - Detection: sidecar in CrashLoopBackOff during termination
  - Mitigations: ensure preStop hooks are not making the container to exit, document best practices
  - Diagnostics: Events
  - Testing: no testing needed as this is a best practice implementing sidecars

###### What steps should be taken if SLOs are not being met to determine the problem?

None.

## Implementation History

<!--
Major milestones in the lifecycle of a KEP should be tracked in this section.
Major milestones might include:
- the `Summary` and `Motivation` sections being merged, signaling SIG acceptance
- the `Proposal` section being merged, signaling agreement on a proposed design
- the date implementation started
- the first Kubernetes release where an initial version of the KEP was available
- the version of Kubernetes where the KEP graduated to general availability
- when the KEP was retired or superseded
-->

- 2018-05-14: First proposal.
- 2023-06-09: Target 1.28 for Alpha.
- 2023-07-08: Alpha implementation merged.
- 1.29: feature is in Beta
- 1.33: feature is graduated to Stable
