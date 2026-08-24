### Risks and Mitigations

1. Backward compatibility: When Pod.Spec.Containers[i].Resources becomes
   representative of desired state, and Pod's actual resource configurations are
   tracked in Pod.Status.ContainerStatuses[i].Resources, applications
   that query PodSpec and rely on Resources in PodSpec to determine resource
   configurations will see values that may not represent actual configurations. As a
   mitigation, this change needs to be documented and highlighted in the
   release notes, and in top-level Kubernetes documents.
1. Scheduler race condition: If a resize happens concurrently with the scheduler evaluating the node
   where the pod is resized, it can result in a node being over-scheduled, which will cause the pod
   to be rejected with an `OutOfCPU` or `OutOfMemory` error. Solving this race condition is out of
   scope for this KEP, but a general solution may be considered in the future.

### Test Plan

<!--
**Note:** *Not required until targeted at a release.*
The goal is to ensure that we don't accept enhancements with inadequate testing.

All code is expected to have adequate tests (eventually with coverage
expectations). Please adhere to the [Kubernetes testing guidelines][testing-guidelines]
when drafting this test plan.

[testing-guidelines]: https://git.k8s.io/community/contributors/devel/sig-testing/testing.md
-->

[x] I/we understand the owners of the involved components may require updates to
existing tests to make this code solid enough prior to committing the changes necessary
to implement this enhancement.

#### Prerequisite testing updates

<!--
Based on reviewers feedback describe what additional tests need to be added prior
implementing this enhancement to ensure the enhancements have also solid foundations.
-->

#### Unit Tests

Unit tests will cover the sanity of code changes that implements the feature,
and the policy controls that are introduced as part of this feature. This is
not exhaustive, but a few specifics are covered below:

##### Allocation Manager
Tests: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/pkg/kubelet/allocation/allocation_manager_test.go

The allocation manager is responsible for determining whether a resize can be allocated.
Unit tests cover this logic, including:
- Resizes with unsupported features such as static cpu/memory memory or swap are marked infeasible.
- Resizes for which the node does not currently have room for are marked as deferred.
- Deferred resizes are retried according to the desired priority. 

##### Kuberuntime Manager
Tests: 
- https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/pkg/kubelet/kuberuntime/kuberuntime_manager_test.go#L3048
- https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/pkg/kubelet/kuberuntime/kuberuntime_manager_test.go#L2320
- https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/pkg/kubelet/kuberuntime/kuberuntime_manager_test.go#L3290
- https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/pkg/kubelet/kuberuntime/kuberuntime_manager_test.go#L3668

The kuberuntime manager is responsible for actuating a resize after it has been allocated.
Unit tests cover this logic, including:
- Validation of the resize, i.e. that memory limits cannot be resized below the usage
- The logic for determining whether a pod resize is in progress (and that the corresponding pod condition gets added)
- Computation of what resize actions need to be performed
- The mock container manager has the expected cgroup values post-resize. 

##### CRI uunit tests

CRI unit tests are updated to reflect use of ContainerResources object in
UpdateContainerResources and ContainerStatus APIs.

#### Integration tests

Comprehensive E2E tests provide good coverage. The following integration tests are also
added for additional coverage: 
- https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/integration/pods/pods_test.go#L852
- https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/integration/scheduler/queueing/queue.go#L287

#### Pod Resize E2E Tests

##### How the tests perform verification

End-to-End tests resize a Pod via PATCH to Pod's Spec.Containers[i].Resources.
The e2e tests use docker as container runtime.
  - Resizing of Requests are verified by querying the values in Pod's
    Status.ContainerStatuses[i].AllocatedResources field.
  - Resizing of Limits are verified by querying the cgroup limits of the Pod's
    containers.
  - Pending resizes have the corresponding condition set in the Pod Status. 
    Completed resizes have their resize status cleared. 

##### Success test cases for Guaranteed Pods with one container

Tests: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/common/node/pod_resize.go#L116-L127

For these tests, all pods had a restartable initContainer attached.

Resize operations performed:
1. Increase, decrease Requests & Limits for CPU only.
1. Increase, decrease Requests & Limits for memory only.
1. Increase, decrease Requests & Limits for CPU and memory in the same direction.
1. Increase, decrease Requests & Limits for CPU and memory in opposite directions.

The following cases are tested against all the above resize operations:
1. No restart policy; no resize of init container.
1. No restart policy + resize of init container.
1. Memory restart policy; no resize of init container.
1. CPU restart policy; no resize of init container.
1. CPU + Memory restart policy; no resize of init container.
1. CPU + Memory restart policy + resize of init container.

##### Success test cases for Guaranteed Pods with multiple containers

Tests: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/common/node/pod_resize.go#L130

1. 3 containers - increase cpu & mem on c1, c2, decrease cpu & mem on c3 - net increase
1. 3 containers - increase cpu & mem on c1, decrease cpu & mem on c2, c3 - net decrease
1. 3 containers - increase: CPU (c1,c3), memory (c2, c3) ; decrease: CPU (c2)

##### Success test cases for Burstable Pods with one container

Tests: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/common/node/pod_resize.go#L208-L220

For these tests, there were no initContainers (since that is covered by the Guaranteed Pods cases).

Resize operations performed:
1. Increase, decrease CPU Requests
1. Increase, decrease CPU Limits
1. Increase, decrease memory Requests
1. Increase, decrease memory Limits
1. Increase, decrease CPU & memory Requests and Limits in the same direction
1. Increase, decrease CPU and memory in opposite directions
1. Increase, decrease Requests & Limits in opposite directions

The following cases are tested against all the above resize operations:
1. No restart policy
1. Memory restart policy
1. CPU restart policy
1. CPU + Memory restart policy

##### Other success test cases for Burstable Pods

Tests: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/common/node/pod_resize.go#L228

1. 6 containers - various operations performed (including adding limits and requests)
1. Resizing with equivalents (e.g. 2m -> 1m)

##### Memory limit decrease

Test: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/common/node/pod_resize.go#L548

This test covers that memory limits can be decreased, but not below the current usage.

##### Patch error tests

Tests: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/common/node/pod_resize.go#L307

These tests cover that the following attempts to patch a pod for resize will be rejected by the API server:
1. Best Effort pod - request memory
1. Best Effort pod - request CPU
1. Guaranteed pod - remove cpu & memory limits
1. Burstable pod - remove cpu & memory limits + increase requests
1. Burstable pod - remove memory requests
1. Burstable pod - remove cpu requests
1. Burstable pod - reorder containers
1. Guaranteed pod - rename containers
1. Burstable pod - set requests == limits
1. Burstable pod - resize ephemeral storage
1. Burstable pod - nonrestartable initContainer

##### Scheduler logic tests

Tests: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/node/pod_resize.go#L494

These tests cover the scheduler logic with respect to in-place pod resize and the defered / infeasible
conditions. The flow of this test is:
1. Create pod1 and pod2 on node such that pod1 has enough CPU to be scheduled, but pod2 does not.
1. Resize pod2 down so that it fits on the node and can be scheduled. 
1. Verify that pod2 gets scheduled and comes up and running.
1. Create pod3 that requests more CPU than available, verify that it is pending.
1. Resize pod1 down so that pod3 gets room to be scheduled.
1. Verify that pod3 is scheduled and running.
1. attempt to scale up pod1 to requests more CPU than available, verify the resize is deferred.
1. Delete pod2 + pod3 to make room for pod3.
1. Verify that pod1 resize has completed.
1. Attempt to scale up pod1 to request more cpu than the node has, verify the resize is infeasible.

##### Retry of deferred resizes

Tests: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/node/pod_resize.go#L690

These tests cover the logic for retrying deferred resizes in the following cases:
1. Deferred resizes succeed after the scale down of another pod. (Deletion case is covered in the previous tests).
1. Deferred resizes are attempted according to the desired priority.
1. Place 4 pods on the node; delete the first one and verify the chain reaction of deferred resizes succeeding. The 
   resources are carefully chosen such that
    - deletion of pod1 should make room for pod2's resize (but not pod3 or pod4).
    - pod2's resize should make room for pod3's resize (but not pod4).
    - pod3's resize should make room for pod4's resize.

##### Resource Quota tests

Tests: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/node/pod_resize.go#L47

1. Exceed max CPU
1. Exceed max memory
1. Exceed max CPU and memory
1. Valid increase of CPU
1. Valid increase of memory
1. Valid increase of CPU and memory

##### Limit Ranger tests

Tests: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/node/pod_resize.go#L218

1. Exceed max CPU
1. Exceed max memory
1. Exceed max CPU and memory
1. Valid increase of CPU
1. Valid increase of memory
1. Valid increase of CPU and memory
1. Go below min CPU
1. Go below min memory
1. Go below min CPU and memory
1. Valid decrease of CPU
1. Valid decrease of memory
1. Valid decrease of CPU and memory

##### Coverage of the READ and REPLACE endpoints

The previous tests are planned to use the PATCH endpoint, but we also need coverage of READ and REPLACE endpoints.
A basic test will be added that uses REPLACE to perform a resize, and the READ endpoint to verify the result.

#### Backward Compatibility and Negative Tests

1. Verify that Node is allowed to update only a Pod's AllocatedResources field.
1. Verify that only Node account is allowed to update AllocatedResources field.
1. Verify that updating Pod Resources in workload template spec retains current
   behavior:
   - Updating Pod Resources in Job template is not allowed.
   - Updating Pod Resources in Deployment template continues to result in Pod
     being restarted with updated resources.
1. Verify Pod updates by older version of client-go doesn't result in current
   values of AllocatedResources and ResizePolicy fields being dropped.
1. Verify that only CPU and memory resources are mutable by user.

### Graduation Criteria

#### Alpha
- In-Place Pod Resouces Update functionality is implemented for running Pods,
- LimitRanger and ResourceQuota handling are added,
- Resize Policies functionality is implemented,
- Unit tests and E2E tests covering basic functionality are added,
- E2E tests covering multiple containers are added.
- UpdateContainerResources API changes are done and tested with containerd
  runtime, backward compatibility is maintained.
- ContainerStatus API changes are done. Tests are ready but not enforced.

#### Beta
- E2E tests covering Resize Policy, LimitRanger, and ResourceQuota are added.
- Negative tests are identified and added.
- A "/resize" subresource is defined and implemented.
- Pod-scoped resources are handled if that KEP is past alpha
- ContainerStatus API change tests are enforced and containerd runtime must comply.
- ContainerStatus API change tests are enforced and Windows runtime should comply.

#### Stable
- VPA integration of feature, `InPlaceOrRecreate` update mode, is moved to beta
- User feedback (ideally from at least two distinct users) is green
- No major bugs reported for three months
- The following tests are promoted to Conformance:
  - Coverage of the READ and REPLACE endpoints (https://github.com/kubernetes/kubernetes/pull/134407)
  - The multi-container tests for guaranteed pods: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/common/node/pod_resize.go#L130
  - The multi-container test for burstable pods: https://github.com/kubernetes/kubernetes/blob/ad82c3d39f5e9f21e173ffeb8aa57953a0da4601/test/e2e/common/node/pod_resize.go#L231

The following items have been removed from the stable graduation criteria:
- In-place pod resize support for pod level resources. Pod level resources is now beta, so the
  lack of support for resize is now a significant missing piece of that functionality; however
  we don't believe this is a strong enough reason to block IPPR GA. We can, however, consider
  whether this should block GA of pod level resources.
- `UpdatePodSandboxResources` is implemented by containerd & CRI-O. This is going to be re-evaluated
  in the context of pod level resources resizing.
- Re-evaluate the following decisions:
  - Resize atomicity: Resizes will stay atomic. Allowing partial resizes adds significant complexity
    and the use case is unclear.
  - Exposing allocated resources in the pod status: We will continue to expose allocated resources in
    the pod status.
  - QOS class changes: This is a large feature with broad implications, so can be considered in a future
    enhancement.

### Upgrade / Downgrade Strategy
Scheduler and API server should be updated before Kubelets in that order.
Kubelet and the runtime versions should use the same CRI version in lock-step.
Upgrade involves draining all pods from a node, installing a CRI runtime with this
version of the API and update to a matching kubelet and making node schedulable again.
Downgrade involves doing the above in reverse.

### Version Skew Strategy
CRI changes were merged in v1.25 in order to enable runtimes to implement support.
  - containerd added support for this feature in 1.6.9

Previous versions of clients that are unaware of the new ResizePolicy fields would set them
to nil. API server mutates such updates by copying non-nil values from old Pod to the current
Pod.

Prior to v1.31, with InPlacePodVerticalScaling disabled, the kubelet interprets mutation to Pod
Resources as a Container definition change and will restart the container with the new Resources.
This could lead to Node resource over-subscription. In v1.31, the kubelet no longer considers
resource changes a change in the pod definition and doesn't restart the container. In this case, the
change to the new resource value happens if the container is restart for any other reason, making
the change non-deterministic and not reflected in the API. Both of these cases are undesirable, so
the API server should reject a resize request if the Kubelet does not support it
(InPlacePodVerticalScaling enabled).

To achieve this, the apiserver will check if the `.status.containerStatuses[*].resources` field is
non-nil on any running containers. This field is set by the kubelet on running containers if and
only if IPPVS is enabled, and can therefore be used as a proxy to determine if the Kubelet running
the pod has the feature enabled. The apiserver logic to determine if a resource mutation is allowed
then becomes:

```go
if !InPlacePodVerticalScaling {
  return false
}
for _, c := range pod.Status.ContainerStatuses {
  if c.State.Running != nil {
    return c.Resources != nil
  }
}
// No running containers
return true
```

Note that even if the container does not specify any resources requests, the status
Resources is still set to the non-nill empty value `{}`.

If a pod has not yet been scheduled, the resize is allowed, and the new values are used when
scheduling & starting the pod.

If a pod has been scheduled but does not have any running containers, there is no signal indicating
whether the assigned node supports resize, so we default to allowing resize. If the node does not
have resize enabled in this case, then a resized container will be started with the new resource
value. It is possible that the node could end up over-provisioned in this case.

It is also possible for a race condition to occur: resize on a non-running container is allowed, but
the Kubelet simultaneously starts the container. The resulting behavior would depend on the version:
prior to v1.31, the container is restarted with the new values. After v1.31, the container continues
running with the old resource values. Since this race condition only exists during enablement skew,
we choose to accept it as a known-issue.

## Production Readiness Review Questionnaire

<!--

Production readiness reviews are intended to ensure that features merging into
Kubernetes are observable, scalable and supportable; can be safely operated in
production environments, and can be disabled or rolled back in the event they
cause increased failures in production. See more in the PRR KEP at
https://git.k8s.io/enhancements/keps/sig-architecture/20190731-production-readiness-review-process.md.

The production readiness review questionnaire must be completed for features in
v1.19 or later, but is non-blocking at this time. That is, approval is not
required in order to be in the release.

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

_This section must be completed when targeting alpha to a release._

* **How can this feature be enabled / disabled in a live cluster?**
  - [x] Feature gate (also fill in values in `kep.yaml`)
    - Feature gate name: `InPlacePodVerticalScaling`
      - Components depending on the feature gate: kubelet, kube-apiserver, kube-scheduler
    - Feature gate name: `InPlacePodVerticalScalingAllocatedStatus`
      - Components depending on the feature gate: kubelet, kube-apiserver
      - Requires `InPlacePodVerticalScaling` be enabled

* **Does enabling the feature change any default behavior?**

  - Kubelet sets several pod status fields: `AllocatedResources`, `Resources`

* **Can the feature be disabled once it has been enabled (i.e. can we roll back
  the enablement)?** Yes

  - `InPlacePodVerticalScaling` can be disabled without issue in the control plane.
  - `InPlacePodVerticalScaling` can be disabled on nodes, but if there are any pending resizes
    container resource configurations may be left in an unknown state. This can be avoided by
    draining the node before disabling in-place resize.
  - `InPlacePodVerticalScalingAllocatedStatus` can be disabled and reenabled without consequence.

* **What happens if we reenable the feature if it was previously rolled back?**
  - API will once again permit modification of Resources for 'cpu' and 'memory'.
  - Actual resources applied will be reflected in in Pod's ContainerStatuses.

* **Are there any tests for feature enablement/disablement?**
  Unit tests and E2E tests.
   - Unit tests verify that feature does not introduce any regression.
   - E2E tests run against a local cluster verify that feature works as expected.

### Rollout, Upgrade and Rollback Planning

_This section must be completed when targeting beta graduation to a release._

* **How can a rollout fail? Can it impact already running workloads?**

  - Failure scenarios are already covered by the version skew strategy.

* **What specific metrics should inform a rollback?**

  - Scheduler indicators:
    - `scheduler_pending_pods`
    - `scheduler_pod_scheduling_attempts`
    - `scheduler_pod_scheduling_duration_seconds`
    - `scheduler_unschedulable_pods`
  - Kubelet indicators:
    - `kubelet_pod_worker_duration_seconds`
    - `kubelet_runtime_operations_errors_total{operation_type=update_container}` 


* **Were upgrade and rollback tested? Was the upgrade->downgrade->upgrade path tested?**

  Testing plan:

  1. Create test pod
  2. Upgrade API server
  3. Attempt resize of test pod
     - Expected outcome: resize is rejected (see version skew section for details)
  4. Create upgraded node
  5. Create second test pod, scheduled to upgraded node
  6. Attempt resize of second test pod
    - Expected outcome: resize successful
  7. Delete upgraded node
  8. Restart API server with feature disabled
    - Ensure original test pod is still running
  9. Attempt resize of original test pod
    - Expected outcome: request rejected by apiserver
  10. Restart API server with feature enabled
    - Verify original test pod is still running

* **Is the rollout accompanied by any deprecations and/or removals of features, APIs,
fields of API types, flags, etc.?**

  No.

### Monitoring Requirements

_This section must be completed when targeting beta graduation to a release._

* **How can an operator determine if the feature is in use by workloads?**

  Metric: `apiserver_request_total{resource=pods,subresource=resize}`

* **How can someone using this feature know that it is working for their instance?**

  - If the Kubelet supports InPlacePodVerticalScaling, it will always set the `Resources` field in
    container status.
  - The `ResizeStatus` in the pod status should converge to the empty value, indicating the resize has completed.
  - The `Resources` in the container status should converge to the resized resources, or an
    approximation of it (see [Actuating Resizes](#actuating-resizes) for more details on
    when these resources can diverge).

* **What are the SLIs (Service Level Indicators) an operator can use to determine
the health of the service?**
  - [x] Metrics
    - Metric name: `apiserver_request_total{resource=pods,subresource=resize}`
      - Components exposing the metric: apiserver
    - Metric name: `runtime_operations_duration_seconds{operation_type=container_update}`
      - Components exposing the metric: kubelet
    - Metric name: `runtime_operations_errors_total{operation_type=container_update}`
      - Components exposing the metric: kubelet

* **What are the reasonable SLOs (Service Level Objectives) for the above SLIs?**

  - Resize requests should succeed (`apiserver_request_total{resource=pods,subresource=resize}` with non-success `code` should be low)
  - Resource update operations should complete quickly (`runtime_operations_duration_seconds{operation_type=container_update} < X` for 99% of requests)
  - Resource update error rate should be low (`runtime_operations_errors_total{operation_type=container_update}/runtime_operations_total{operation_type=container_update}`)

* **Are there any missing metrics that would be useful to have to improve observability
of this feature?**

  - ~~Kubelet admission rejections: https://github.com/kubernetes/kubernetes/issues/125375~~ (DONE)
  - Resize operate duration (time from the Kubelet seeing the request to actuating the changes): this would require persisting more state about when the resize was first observed.

### Dependencies

_This section must be completed when targeting beta graduation to a release._

* **Does this feature depend on any specific services running in the cluster?**

  Compatible container runtime (see [CRI changes](#cri-changes)).

### Scalability

_For alpha, this section is encouraged: reviewers should consider these questions
and attempt to answer them._

_For beta, this section is required: reviewers must answer these questions._

_For GA, this section is required: approvers should be able to confirm the
previous answers based on experience in the field._

* **Will enabling / using this feature result in any new API calls?** Yes
  Describe them, providing:
  - API call type (e.g. PATCH pods)
    - One new PATCH PodStatus API call in response to Pod resize request.
    - No additional overhead unless Pod resize is invoked.
  - estimated throughput
  - originating component(s) (e.g. Kubelet, Feature-X-controller)
    - Kubelet
  focusing mostly on:
  - components listing and/or watching resources they didn't before
  - API calls that may be triggered by changes of some Kubernetes resources
    (e.g. update of object X triggers new updates of object Y)
  - periodic API calls to reconcile state (e.g. periodic fetching state,
    heartbeats, leader election, etc.)

* **Will enabling / using this feature result in introducing new API types?** No
  Describe them, providing:
  - API type
  - Supported number of objects per cluster
  - Supported number of objects per namespace (for namespace-scoped objects)

* **Will enabling / using this feature result in any new calls to the cloud
provider?** No

* **Will enabling / using this feature result in increasing size or count of
the existing API objects?** Yes
  Describe them, providing:
  - API type(s):
  - Estimated increase in size: (e.g., new annotation of size 32B)
  - Estimated amount of new objects: (e.g., new Object X for every existing Pod)
    - type Container has new field ResizePolicy, a list that adds upto 50 bytes.
    - type PodStatus has a new field, a list that adds upto 32 bytes.
    - type ContainerStatus has new field of type v1.ResourceList that mirrors
      Container.Resources.Requests in size.
    - type ContainerStatus has new field of type v1.ResourceRequirements that
      mirrors Container.Resources in size.

* **Will enabling / using this feature result in increasing time taken by any
operations covered by [existing SLIs/SLOs]?** No
  Think about adding additional work or introducing new steps in between
  (e.g. need to do X to start a container), etc. Please describe the details.

* **Will enabling / using this feature result in non-negligible increase of
resource usage (CPU, RAM, disk, IO, ...) in any components?** No
  Things to keep in mind include: additional in-memory state, additional
  non-trivial computations, excessive access to disks (including increased log
  volume), significant amount of data sent and/or received over network, etc.
  This through this both in small and large cases, again with respect to the
  [supported limits].

* **Can enabling / using this feature result in resource exhaustion of some node resources (PIDs, sockets, inodes, etc.)?** No

### Troubleshooting

The Troubleshooting section currently serves the `Playbook` role. We may consider
splitting it into a dedicated `Playbook` document (potentially with some monitoring
details). For now, we leave it here.

_This section must be completed when targeting beta graduation to a release._

* **How does this feature react if the API server and/or etcd is unavailable?**

  - If the API is unavailable prior to the resize request being made, the request wil not go through.
  - If the API is unavailable before the Kubelet observes the resize, the request will remain pending until the Kubelet sees it.
  - If the API is unavailable after the Kubelet observes the resize, then the pod status may not
    accurately reflect the running pod state. The Kubelet tracks the resource state internally.

* **What are other known failure modes?**

  - Race condition with scheduler can cause pods to be rejected with `OutOfCPU` or
    `OutOfMemory`.
  - Race condition with pod startup on version-skewed clusters can lead to pods running in an
    unknown resource configuration. See [Version Skew Strategy](#version-skew-strategy) for more
    details.
  - Shrinking memory limit below memory usage can leave the resize in an `InProgress` state
    indefinitely. Race conditions around reading usage info could cause container to OOM on resize.

* **What steps should be taken if SLOs are not being met to determine the problem?**

  - Investigate Kubelet and/or container runtime logs.

[supported limits]: https://git.k8s.io/community//sig-scalability/configs-and-limits/thresholds.md
[existing SLIs/SLOs]: https://git.k8s.io/community/sig-scalability/slos/slos.md#kubernetes-slisslos

## Implementation History

- 2018-11-06 - initial KEP draft created
- 2019-01-18 - implementation proposal extended
- 2019-03-07 - changes to flow control, updates per review feedback
- 2019-08-29 - updated design proposal
- 2019-10-25 - Initial CRI changes KEP draft created
- 2019-10-25 - update key open items and move KEP to implementable
- 2020-01-06 - API review suggested changes incorporated
- 2020-01-13 - Test plan and graduation criteria added
- 2020-01-14 - CRI changes test plan and graduation criteria added
- 2020-01-21 - Graduation criteria updated per review feedback
- 2020-11-06 - Updated with feedback from reviews
- 2020-12-09 - Add "Deferred"
- 2021-02-05 - Final consensus on allocatedResources[] and resize[]
- 2022-05-01 - KEP 2273-kubelet-container-resources-cri-api-changes merged with this KEP
- 2023-04-08 - Catch up KEP details to what is actually implemented
- 2024-10-09 - v1.32 updates for planned beta
    - Remove container-level status `AllocatedResources`
    - Add `/resize` subresource specification
    - Make `ResizePolicy` mutable
    - Introduce best-effort `UpdatePodSandboxResources` CRI call
    - Add sidecar resize support
    - Describe the [Atomic Resizes](#atomic-resizes) principle
    - Add ResourceQuota details
    - Heuristic version skew handling in API validation
- 2025-01-24 - v1.33 updates for planned beta
    - Replace ResizeStatus with conditions
    - Improve memory limit downsize handling
    - Rename ResizeRestartPolicy `NotRequired` to `PreferNoRestart`,
      and update CRI `UpdateContainerResources` contract
    - Add back `AllocatedResources` field to resolve a scheduler corner case
    - Introduce Actuated resources for actuation
- 2025-06-03 - v1.34 post-beta updates
    - Allow no-restart memory limit decreases
    - Add instrumentation section
    - Priority of resize requests
- 2025-09-22 - Correct KEP details to match actual implementation
    - revert PreferNoRestart resize policy back to NotRequired
    - add more details about the resize status
    - document kubelet-triggered eviction for critical pods
    - update outdated notes regarding static CPU
    - correct details about instrumentation
- 2025-10-15 - Update in-place pod resize for GA
    - Update test plan
    - Remove `UpdatePodSandboxResources` from graduation criteria 
- 2025-12-29 - Mark as implemented after GA release
