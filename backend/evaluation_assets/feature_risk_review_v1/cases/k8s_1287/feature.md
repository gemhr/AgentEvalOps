## Summary

This proposal aims at allowing Pod resource requests & limits to be updated
in-place, without a need to restart the Pod or its Containers.

The **core idea** behind the proposal is to make PodSpec mutable with regards to
Resources, denoting **desired** resources. Additionally, PodStatus is extended to
provide information about **actual** resources applied to the Pod and its Containers.

This document builds upon [proposal for live and in-place vertical scaling][]
and [Vertical Resources Scaling in Kubernetes][].

[proposal for live and in-place vertical scaling]:
https://github.com/kubernetes/community/pull/1719
[Vertical Resources Scaling in Kubernetes]:
https://docs.google.com/document/d/18K-bl1EVsmJ04xeRq9o_vfY2GDgek6B6wmLjXw-kos4

This proposal also aims to improve the Container Runtime Interface (CRI) APIs for
managing a Container's CPU and memory resource configurations on the runtime.
It seeks to extend UpdateContainerResources CRI API such that it works for
Windows, and other future runtimes besides Linux. It also seeks to extend
ContainerStatus CRI API to allow Kubelet to discover the current resources
configured on a Container.

## Motivation

Resources allocated to a Pod's Container(s) can require a change for various
reasons:
* load handled by the Pod has increased significantly, and current resources
  are not sufficient,
* load has decreased significantly, and allocated resources are unused,
* resources have simply been set improperly.

Currently, changing resource allocation requires the Pod to be recreated since
the PodSpec's Container Resources is immutable.

While many stateless workloads are designed to withstand such a disruption,
some are more sensitive, especially when using low number of Pod replicas.

Moreover, for stateful or batch workloads, Pod restart is a serious disruption,
resulting in lower availability or higher cost of running.

Allowing Resources to be changed without recreating the Pod or restarting the
Containers addresses this issue directly.

Additionally, In-Place Pod Vertical Scaling feature relies on Container Runtime
Interface (CRI) to update CPU and/or memory requests/limits for a Pod's Container(s).

The current CRI API set has a few drawbacks that need to be addressed:
1. UpdateContainerResources CRI API takes a parameter that describes Container
   resources to update for Linux Containers, and this may not work for Windows
   Containers or other potential non-Linux runtimes in the future.
1. There is no CRI mechanism that lets Kubelet query and discover the CPU and
   memory limits configured on a Container from the Container runtime.
1. The expected behavior from a runtime that handles UpdateContainerResources
   CRI API is not very well defined or documented.

### Goals

* Primary: allow to change container resource requests & limits without
  necessarily restarting the container.
* Secondary: allow actors (users, VPA, StatefulSet, JobController) to decide
  how to proceed if in-place resource resize is not possible.
* Secondary: allow users to specify which Containers can be resized without a
  restart.

Additionally, this proposal has two goals for CRI:
  - Modify UpdateContainerResources to allow it to work for Windows Containers,
    as well as Containers managed by other runtimes besides Linux,
  - Provide CRI API mechanism to query the Container runtime for CPU and memory
    resource configurations that are currently applied to a Container.

An additional goal of this proposal is to better define and document the
expected behavior of a Container runtime when handling resource updates.

### Non-Goals

The explicit non-goal of this KEP is to avoid controlling full lifecycle of a
Pod which failed in-place resource resizing. This should be handled by actors
which initiated the resizing.

Other identified non-goals are:
* allow to change Pod QoS class
* to change resources of non-restartable InitContainers
* eviction of lower priority Pods to facilitate Pod resize
* updating extended resources or any other resource types besides CPU, memory
* support for CPU/memory manager policies besides the default 'None' policy
* resolving race conditions with the scheduler

Definition of expected behavior of a Container runtime when it handles CRI APIs
related to a Container's resources is intended to be a high level guide.  It is
a non-goal of this proposal to define a detailed or specific way to implement
these functions. Implementation specifics are left to the runtime, within the
bounds of expected behavior.

## Proposal

### API Changes

Container resource requests & limits can now be mutated via the `/resize` pod subresource.
PodStatus is extended to show the resources applied to the Pod and its Containers.

* Pod.Spec.Containers[i].Resources becomes purely a declaration, denoting the
  **desired** state of Pod resources
* Pod.Status.ContainerStatuses[i].Resources (new field, type
  v1.ResourceRequirements) shows the **actual** resources held by the Pod and
  its Containers for running containers, and the allocated resources for non-running containers.
* Pod.Status.ContainerStatuses[i].AllocatedResources (new field, type v1.ResourceList) reports the
  allocated resource requests.
* Pod.Status.Conditions explain what is happening for a given resource on a given container (see
  details [below](#resize-status)).

The actual resources are reported by the container runtime in some cases, and for all other
resources are copied from the latest snapshot of allocated resources. Currently, the resources
reported by the runtime are CPU Limit (translated from quota & period), CPU Request (translated from
shares), and Memory Limit.

Additionally, a new `Pod.Spec.Containers[i].ResizePolicy[]` field (type
`[]v1.ContainerResizePolicy`) governs whether containers need to be restarted on resize. See
[Container Resize Policy](#container-resize-policy) for more details.

#### Allocated Resources

When the Kubelet admits a pod initially or admits a resize, all resource requirements from the spec
are cached and checkpointed locally. When a container is (re)started, these are the requests and
limits used. Only the allocated requests are reported in the API, through the
`Pod.Status.ContainerStatuses[i].AllocatedResources` field.

The scheduler uses `max(spec...resources, status...allocatedResources, status...resources)` for fit
decisions, but since the actual resources are only relevant and reported for running containers, the
Kubelet sets `status...resources` equal to the allocated resources for non-running containers.

#### Subresource

Resource changes can only be made via the new `/resize` subresource, which accepts Update and Patch
verbs. The request & response types for this subresource are the full pod object, but only the
following fields are allowed to be modified:

* `.spec.containers[*].resources`
* `.spec.initContainers[*].resources` (only for sidecars)
* `.spec.resizePolicy`

#### Validation

Resource fields remain immutable via pod update (a change from the alpha behavior), but are mutable
via the new `/resize` subresource. The following API validation rules will be applied for updates via
the `/resize` subresource:

1. Resources & ResizePolicy must be valid under pod create validation.
2. Computed QOS class cannot change. See [QOS Class](#qos-class) for more details.
3. Running pods without the `Pod.Status.ContainerStatuses[i].Resources` field set cannot be resized.
   See [Version Skew Strategy](#version-skew-strategy) for more details.

#### Container Resize Policy

To provide fine-grained user control, PodSpec.Containers is extended with
ResizeRestartPolicy - a list of named subobjects (new object) that supports
'cpu' and 'memory' as names. It supports the following restart policy values:
* `NotRequired` - default value; resize the Container without restart, if possible.
* `RestartContainer` - the container requires a restart to apply new resource values.
  (e.g.  Java process needs to change its Xmx flag) By using ResizePolicy, user
  can mark Containers as safe (or unsafe) for in-place resource update. Kubelet
  uses it to determine the required action.

Note: `NotRequired` restart policy for resize does not *guarantee* that a container won't be
restarted. If the runtime knows a resize will trigger a restart, it should return an error instead,
and the Kubelet will retry the resize on the next pod sync. The behavior when shrinking
memory limits is defined under [Memory Limit Decreases](#memory-limit-decreases) below.

Setting the flag to separately control CPU & memory is due to an observation
that usually CPU can be added/removed without much problem whereas changes to
available memory are more probable to require restarts.

If more than one resource type with different policies are updated at the same
time, then `RestartContainer` policy takes precedence over `NotRequired` policy.

If a pod's RestartPolicy is `Never`, the ResizePolicy fields must be set to
`NotRequired` to pass validation.  That said, any in-place resize may result
in the container being stopped *and not restarted*, if the system can not
perform the resize in place.

The `ResizePolicy` field is immutable.

#### Resize Status

Resize status will be tracked via 2 new pod conditions: `PodResizePending` and `PodResizeInProgress`.

**PodResizePending** will track states where the spec has been resized, but the Kubelet has not yet
allocated the resources (desired resources != actuated resources). There are two reasons associated 
with this condition:

* `Deferred` - the proposed resize is feasible in theory (it fits on this node)
  but is not possible right now; it will be regularly reevaluated. This can happen
  if the node does not have enough free resources at the moment, but might in the 
  future when other pods are removed or scaled down.
* `Infeasible` - the proposed resize is not feasible and is rejected; it will never
  be re-evaluated. Today, the possible reasons for infeasible include:
  * The requested resources exceed the node's total capacity.
  * The pod is a static pod.
  * In-place resize is not yet supported for containers with swap enabled.
  * In-place resize is not yet supported for guaranteed pods alongside memory manager static policy.
  * In-place resize is not yet supported for guaranteed pods alongside CPU manager static policy.

In either case, the condition's `message` will include details of why the resize has not been
admitted. `lastTransitionTime` will be populated with the time the condition was added. `status`
will always be `True` when the condition is present - if there is no longer a pending resized
(either the resize was allocated or reverted), the condition will be removed. `observedGeneration` will
reflect the `metadata.generation` of the pod when the resize was last attempted.

**PodResizeInProgress** will track in-progress resizes, and should be present whenever allocated resources
!= actuated resources (see [Resource States](#resource-states)). For successful synchronous
resizes, this condition should be short lived, and `reason` and `message` will be left blank. If an
error occurs while actuating the resize, the `reason` will be set to `Error`, and `message` will be
populated with the error message. In the future, this condition will also be used for long-running
resizing behaviors (see [Memory Limit Decreases](#memory-limit-decreases)). `observedGeneration` will
reflect the `metadata.generation` of the pod when the resize was initially requested.

Note that it is possible for both conditions to be present at the same time, for example if an error
is encountered while actuating a resize and a new resize comes in that gets deferred.

Prior to v1.33, the resize status was tracked by a dedicated `Pod.Status.Resize` field. This field
will be deprecated, and not graduate to beta.

#### CRI Changes

As of Kubernetes v1.20, the CRI has included support for in-place resizing of containers via the
`UpdateContainerResources` API, which is implemented by both containerd and CRI-O. Additionally, the
`ContainerStatus` message includes a `ContainerResources` field, which reports the current resource
configuration of the container. `UpdateContainerResources` must be idempotent, if called with the
same configuration multiple times.

Starting with Kubernetes v1.33, the contract on the `UpdateContainerResources` call will be updated
to specify that runtimes should not deliberately restart the container to adjust the resources. If a
restart is required to resize, the runtime should return an error instead. There may be edge-cases
where a restart can still be triggered (see [Memory Limit Decreases](#memory-limit-decreases)), so
this is a best-effort requirement. There is no enforcement of this behavior.

Even though pod-level cgroups are currently managed by the Kubelet, runtimes may rely need to be
notified when the resource configuration changes. For example, this information should be passed
through to NRI plugins. To this end, we will add a new `UpdatePodSandboxResources` API:

```proto
service RuntimeService {
  ...

  // UpdatePodSandboxResources synchronously updates the PodSandboxConfig with
  // the pod-level resource configuration. This method is called _after_ the
  // Kubelet reconfigures the pod-level cgroups.
  // This request is treated as best effort, and failure will not block the
  // Kubelet with proceeding with a resize.
  rpc UpdatePodSandboxResources(UpdatePodSandboxResourcesRequest) returns (UpdatePodSandboxResourcesResponse) {}
}

message UpdatePodSandboxResourcesRequest {
    // ID of the PodSandbox to update.
    string pod_sandbox_id = 1;

    // Optional overhead represents the overheads associated with this sandbox
    LinuxContainerResources overhead = 2;
    // Optional resources represents the sum of container resources for this sandbox
    LinuxContainerResources resources = 3;
}

message UpdatePodSandboxResourcesResponse {}
```

The Kubelet will call `UpdatePodSandboxResources` _after_ it has reconfigured the pod-level cgroups.
This ordering is consistent with pod creation, where the Kubelet configures the pod-level cgroups
before calling `RunPodSandbox`.

For now, the `UpdatePodSandboxResources` call will be treated as best-effort by the Kubelet. This
means that in the case of an error, Kubelet will log the the error but otherwise ignore it and
proceed with the resize.

Note: Windows resources are not included here since they are not present in the
WindowsPodSandboxConfig.

## Design Details

### Resource States

In-place pod resizing adds a lot of new resource states. These are detailed in other sections of
this KEP, but summarized here to help understand how they relate to each other.

The Kubelet now tracks 4 sets of resources for each pod/container:

1. Desired resources
    - What the user (or controller) asked for
    - Recorded in the API as the spec resources (`.spec.container[i].resources`)
2. Allocated resources
    - The resources that the Kubelet admitted, and intends to actuate
    - Reported in the API through the `.status.containerStatuses[i].allocatedResources` field
      (allocated requests only)
    - Persisted locally on the node (requests + limits) in a checkpoint file
3. Actuated resources
    - The resource configuration that the Kubelet passed to the runtime to actuate
    - Not reported in the API
    - Persisted locally on the node in a checkpoint file
    - See [Actuating Resizes](#actuating-resizes) for more details
4. Actual resources
    - The actual resource configuration the containers are running with, reported by the runtime,
      typically read directly from the cgroup configuration
    - Reported in the API via the `.status.conatinerStatuses[i].resources` field
        - _Note: for non-running contiainers `.status.conatinerStatuses[i].resources` will be the Allocated resources._

Changes are always propogated through these 4 resource states in order:

```
Desired --> Allocated --> Actuated --> Actual
```

### Priority of Resize Requests

Resize requests detected by the kubelet (in `HandlePodUpdates` and `HandlePodAdditions`)
will be added to a queue of pending resizes. Resize requests will be attempted according to
the following priority:

1. *Resource requests are not increasing*: Resizes that don't increase requests will be
prioritized first. These resizes are expected to always succeed and would not be marked as
pending.
2. *PriorityClass*: Pods with a higher PriorityClass.
3. *QoS Class*: Pods with a higher QoS class, where Guaranteed > Burstable. Best effort pods 
do not have CPU or memory resources, so are excluded from the discussion here.
4. *Time since resize request*: If all else is the same, resizes that have been pending
longer will be retried first (leveraging LastTransitionTime on the PodResizePending condition).

These priorities are *only* used to indicate which resize requests will be attempted first. 
Scheduler preemption/eviction to make room for pending resizes is not in scope. 

A higher priority resize being marked as pending should not block the remaining pending resizes
from being attempted, i.e. we will try all remaining resizes in the queue even if one is unsuccessful.
Resizes that are deferred will be added back to the queue to be re-attempted later. Resizes that are
infeasible may never be retried.

Allocation will be attempted on the pods in the queue:
- At the end of `HandlePodUpdates`, `HandlePodRemoves`, and `HandlePodCleanups` when a change to the queue is detected.
- Upon completion of another resize request.
- Periodically, to catch any cases that we may have missed.

A successful allocation will trigger a pod sync, which will actuate the allocated resize and update the
pod status accordingly.

### Kubelet-triggered eviction

A pod can be marked as critical with the `priorityClassName` of `system-node-critical` or `system-cluster-critical` as
described in [Guaranteed Scheduling For Critical Add-On Pods](https://kubernetes.io/docs/tasks/administer-cluster/guaranteed-scheduling-critical-addon-pods/#marking-pod-as-critical). If the kubelet receives a resize request for a
critical pod and there is not enough space for the resize, it will evict a non-critical pod to make room.

### Kubelet and API Server Interaction

When a new Pod is created, Scheduler is responsible for selecting a suitable
Node that accommodates the Pod.

For a newly created Pod, `(Init)ContainerStatuses` will be nil until the Pod is
scheduled to a node. When Kubelet admits a Pod, it will record the admitted
requests & limits to its internal allocated resources checkpoint.

When a Pod resize is requested, Kubelet attempts to update the resources
allocated to the Pod and its Containers. Kubelet first checks if the new desired
resources can fit the Node allocable resources by computing the sum of resources
allocated for all Pods in the Node, except the Pod being resized. For the Pod
being resized, it adds the new desired resources (i.e
Spec.Containers[i].Resources.Requests) to the sum.

* If new desired resources fit, Kubelet accepts the resize, updates the allocated resources, and
  adds the `PodResizeInProgress` condition.  It then invokes the UpdateContainerResources CRI API to update
  Container resource limits.  Once all Containers are successfully updated, it updates
  Status...Resources to reflect new resource values and removes the condition.
* If new desired resources don't fit, Kubelet will add the `PodResizePending` condition with type
  `Infeasible` and a message explaining why.
* If new desired resources fit but are in-use at the moment, Kubelet will add the `PodResizePending`
  condition with type `Deferred` and a message explaining why.

In addition to the above, kubelet will generate Events on the Pod whenever a
resize is accepted or rejected, and if possible at key steps during the resize
process.  This will allow humans to know that progress is being made.

If multiple Pods need resizing, they are handled sequentially in an order
defined by the Kubelet (e.g. in order of arrivial).

Scheduler may, in parallel, assign a new Pod to the Node because it uses cached
Pods to compute Node allocable values. If this race condition occurs, Kubelet
resolves it by rejecting that new Pod if the Node has no room after Pod resize.

Note: After a Pod is rejected, the scheduler could try to reschedule the
replacement pod on the same node that just rejected it.  This is a general
statement about Kubernetes and is outside the scope of this KEP.

#### Kubelet Restart Tolerance

If Kubelet were to restart amidst handling a Pod resize, then upon restart, all
Pods are re-admitted based on their current allocated resources (restored from
 checkpoint). Pending resizes are handled after all existing Pods have been
added. This ensures that resizes don't affect previously admitted existing Pods.

### Scheduler and API Server Interaction

Scheduler continues to use Pod's Spec.Containers[i].Resources.Requests for
scheduling new Pods, and continues to watch Pod updates, and updates its cache.

To compute the Node resources allocated to Pods, pending resizes must be factored in.
The scheduler will use the maximum of:
1. Desired resources, computed from container requests in the pod spec, unless the resize is marked as `Infeasible`
1. Actual resources, computed from the `.status.containerStatuses[i].resources.requests`
1. Allocated resources, reported in `.status.containerStatuses[i].allocatedResources`

### Flow Control

The following steps denote the flow of a series of in-place resize operations
for a Pod with ResizePolicy set to NotRequired for all its Containers.
This is intentionally hitting various edge-cases for demonstration.

1. A new pod is created
    - `spec.containers[0].resources.requests[cpu]` = 1
    - `spec.containers[0].resizePolicy[cpu].restartPolicy` = `"NotRequired"`
    - all status is unset

1. Pod is scheduled
    - `spec.containers[0].resources.requests[cpu]` = 1
    - status still mostly unset

1. kubelet runs the pod and updates the API
    - `spec.containers[0].resources.requests[cpu]` = 1
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1
    - `actuated[cpu]` = 1
    - `status.containerStatuses[0].resources.requests[cpu]` = 1
    - actual CPU shares = 1024

1. Resize #1: cpu = 1.5 (via PUT or PATCH to /resize)
    - apiserver validates the request (e.g. `limits` are not below
      `requests`, ResourceQuota not exceeded, etc) and accepts the operation
    - `spec.containers[0].resources.requests[cpu]` = 1.5
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1
    - `actuated[cpu]` = 1
    - `status.containerStatuses[0].resources.requests[cpu]` = 1
    - actual CPU shares = 1024

1. Kubelet Restarts!
    - The allocated & actuated resources are read back from checkpoint
    - Pods are resynced from the API server, but admitted based on the allocated resources
    - `spec.containers[0].resources.requests[cpu]` = 1.5
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1
    - `actuated[cpu]` = 1
    - `status.containerStatuses[0].resources.requests[cpu]` = 1
    - actual CPU shares = 1024

1. Kubelet syncs the pod, sees resize #1 and admits it
    - `spec.containers[0].resources.requests[cpu]` = 1.5
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1.5
    - `actuated[cpu]` = 1
    - `status.containerStatuses[0].resources.requests[cpu]` = 1
    - `status.conditions[type==PodResizeInProgress]` added
    - actual CPU shares = 1024

1. Resize #2: cpu = 2
    - apiserver validates the request and accepts the operation
    - `spec.containers[0].resources.requests[cpu]` = 2
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1.5
    - `status.containerStatuses[0].resources.requests[cpu]` = 1
    - `status.conditions[type==PodResizeInProgress]`
    - actual CPU shares = 1024

1. Container runtime applied cpu=1.5
    - `spec.containers[0].resources.requests[cpu]` = 2
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1.5
    - `actuated[cpu]` = 1.5
    - `status.containerStatuses[0].resources.requests[cpu]` = 1
    - `status.conditions[type==PodResizeInProgress]`
    - actual CPU shares = 1536

1. kubelet syncs the pod, and sees resize #2 (cpu = 2)
    - kubelet decides this is feasible, but currently insufficient available resources
    - `spec.containers[0].resources.requests[cpu]` = 2
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1.5
    - `actuated[cpu]` = 1.5
    - `status.containerStatuses[0].resources.requests[cpu]` = 1.5
    - `status.conditions[type==PodResizePending].type` = `"Deferred"`
    - `status.conditions[type==PodResizeInProgress]` removed
    - actual CPU shares = 1536

1. Resize #3: cpu = 1.6
    - apiserver validates the request and accepts the operation
    - `spec.containers[0].resources.requests[cpu]` = 1.6
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1.5
    - `actuated[cpu]` = 1.5
    - `status.containerStatuses[0].resources.requests[cpu]` = 1.5
    - `status.conditions[type==PodResizePending].type` = `"Deferred"`
    - actual CPU shares = 1536

1. Kubelet syncs the pod, and sees resize #3 and admits it
    - `spec.containers[0].resources.requests[cpu]` = 1.6
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1.6
    - `actuated[cpu]` = 1.5
    - `status.containerStatuses[0].resources.requests[cpu]` = 1.5
    - `status.conditions[type==PodResizePending]` removed
    - `status.conditions[type==PodResizeInProgress]` added
    - actual CPU shares = 1536

1. Container runtime applied cpu=1.6
    - `spec.containers[0].resources.requests[cpu]` = 1.6
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1.6
    - `actuated[cpu]` = 1.6
    - `status.containerStatuses[0].resources.requests[cpu]` = 1.5
    - `status.conditions[type==PodResizeInProgress]`
    - actual CPU shares = 1638

1. Kubelet syncs the pod
    - `spec.containers[0].resources.requests[cpu]` = 1.6
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1.6
    - `actuated[cpu]` = 1.6
    - `status.containerStatuses[0].resources.requests[cpu]` = 1.6
    - `status.conditions[type==PodResizeInProgress]` removed
    - actual CPU shares = 1638

1. Resize #4: cpu = 100
    - apiserver validates the request and accepts the operation
    - `spec.containers[0].resources.requests[cpu]` = 100
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1.6
    - `actuated[cpu]` = 1.6
    - `status.containerStatuses[0].resources.requests[cpu]` = 1.6
    - actual CPU shares = 1638

1. Kubelet syncs the pod, and sees resize #4
    - this node does not have 100 CPUs, so kubelet cannot admit it
    - `spec.containers[0].resources.requests[cpu]` = 100
    - `status.containerStatuses[0].allocatedResources[cpu]` = 1.6
    - `actuated[cpu]` = 1.6
    - `status.containerStatuses[0].resources.requests[cpu]` = 1.6
    - `status.conditions[type==PodResizePending].type` = `"Infeasible"`
    - actual CPU shares = 1638

#### Container resource limit update ordering

When in-place resize is requested for multiple Containers in a Pod, Kubelet
updates resource limit for the Pod and its Containers in the following manner:
  1. If resource resizing results in net-increase of a resource type (CPU or
     Memory), Kubelet first updates Pod-level cgroup limit for the resource
     type.
  1. All container limit decreases are applied.
  1. If all container limit decreases succeeded and resource resizing results in net-decrease of a
     resource type, Kubelet then updates the Pod-level cgroup limit.
  1. If all previous steps succeeded, container limit increases are applied.

In all the above cases, Kubelet applies Container resource limit decreases
before applying limit increases.

#### Container resource limit update failure handling

If an `UpdateContainerResources` request fails while container limit decreases are being applied,
the remainder of the container limit decreases will be attempted, but container limit increases or
pod limit decreases will not. This ensures that sum of the container limits does not exceed
Pod-level cgroup limit at any point.

If an `UpdateContainerResources` request fails while container limit increases are being applied,
the remaining container limit increases will still be attempted.

If any errors are raised during the resize process:
- An event will be emitted with the error details
- The ResizeStatus will be set to `Error`
- The pod will be requeued for sync, and the resize will be retried on the next pod sync.

#### CRI Changes Flow

Below diagram is an overview of Kubelet using UpdateContainerResources and
ContainerStatus CRI APIs to set new container resource limits, and update the
Pod Status in response to user changing the desired resources in Pod Spec.

```
   +-----------+                   +-----------+                  +-----------+
   |           |                   |           |                  |           |
   | apiserver |                   |  kubelet  |                  |  runtime  |
   |           |                   |           |                  |           |
   +-----+-----+                   +-----+-----+                  +-----+-----+
         |                               |                              |
         |       watch (pod update)      |                              |
         |------------------------------>|                              |
         |     [Containers.Resources]    |                              |
         |                               |                              |
         |                            (admit)                           |
         |                               |                              |
         |                               |  UpdateContainerResources()  |
         |                               |----------------------------->|
         |                               |                         (set limits)
         |                               |<- - - - - - - - - - - - - - -|
         |                               |                              |
         |                               |      ContainerStatus()       |
         |                               |----------------------------->|
         |                               |                              |
         |                               |     [ContainerResources]     |
         |                               |<- - - - - - - - - - - - - - -|
         |                               |                              |
         |      update (pod status)      |                              |
         |<------------------------------|                              |
         | [ContainerStatuses.Resources] |                              |
         |                               |                              |

```

* Kubelet invokes UpdateContainerResources() CRI API in ContainerManager
  interface to configure new CPU and memory limits for a Container by
  specifying those values in ContainerResources parameter to the API. Kubelet
  sets ContainerResources parameter specific to the target runtime platform
  when calling this CRI API.

* Kubelet calls ContainerStatus() CRI API in ContainerManager interface to get
  the CPU and memory limits applied to a Container. It uses the values returned
  in ContainerStatus.Resources to update ContainerStatuses[i].Resources.Limits
  for that Container in the Pod's Status.

#### Kubelet Restart Analysis

Analysis of Kubelet restarts happening at various points of resize, and how recovery happens.
Impacts of a restart outside of resource configuration are out of scope.

1. Kubelet Admits a new pod
  - Resource allocation checkpointed before sending the pod to the pod workers
  - Restart before checkpointing: pod goes through admission again as if new
  - Restart after checkpointing: pod goes through admission using the allocated resources
1. Kubelet creates a container
  - Resources actuated after CreateContainer call succeeds
  - Restart before acknowledgement: Kubelet issues a superfluous UpdatePodResources request
  - Restart after acknowledgement: No resize needed
1. Container starts, triggering a pod sync event
  - Kubelet updates status with actual resources reported by runtime, allocated resources from checkpoint
  - Allocated == Acknowledeged, so no resize needed
  - No races around restart.
1. Pod is resized in the API, Kubelet observes the update
  - Triggers a pod sync
  - On restart, Kubelet reads the latest pod from the API and triggers a pod sync, so same effect as
    observing the update.
1. Updated pod is synced: Check if pod can be admitted
  - No: add `PodResizePending` condition with type `Deferred`, no change to allocated resources
    - Restart: redo admission check, still deferred.
  - Yes: add `PodResizeInProgress` condition, update allocated checkpoint
    - Restart before update: readmit, then update allocated
    - Restart after update: allocated != actuated --> proceed with resize
1. Allocated != Actuated
  - Trigger an `UpdateContainerResources` CRI call, then update Actuated resources on success
  - Restart before CRI call: allocated != actuated, will still trigger the update call
  - Restart after CRI call, before actuated update: will redo update call
  - Restart after actuated update: allocated == actuated, condition removed
  - In all restart cases, `LastTransitionTime` is propagated from the old pod status `PodResizeInProgress`
    condition, and remains unchanged.
1. PLEG updates PodStatus cache, triggers pod sync
  - Pod status updated with actual resources, `PodResizeInProgress` condition removed
  - Desired == Allocated == Actuated, no resize changes needed.

#### Notes

* To avoid races and possible gamification, all components will use Pod's
  Status.ContainerStatuses[i].Resources when computing resources used
  by Pods.
* If additional resize requests arrive when a Pod is being resized, those
  requests are handled after completion of the resize that is in progress. And
  resize is driven towards the latest desired state.
* Impact of Pod Overhead: Kubelet adds Pod Overhead to the resize request to
  determine if in-place resize is possible.
* At this time, Vertical Pod Autoscaler should not be used with Horizontal Pod
  Autoscaler on CPU, memory. This enhancement does not change that limitation.

### Lifecycle Nuances

* Terminated containers can be "resized" in that the resize is permitted by the API, and the Kubelet
  will accept the changes. This makes race conditions where the container terminates around the
  resize "fail open", and prevents a resize of a terminated container from blocking the resize of a
  running container (see [Atomic Resizes](#atomic-resizes)).
* Resizing pods in a graceful shutdown state is permitted, and will be actuated best-effort.

### Atomic Resizes

A single resize request can change multiple values, including any or all of:
* Multiple resource types
* Requests & Limits
* Multiple containers

These resource requests & limits can have interdependencies that Kubernetes may not be aware of. For
example, two containers (in the same pod) coordinating work may need to be scaled in tandem. It probably doesn't makes
sense to scale limits independently of requests, and scaling CPU without memory could just waste
resources. To mitigate these issues and simplify the design, the Kubelet will treat the requests &
limits for all containers in the spec as a single atomic request, and won't accept any of the
changes unless all changes can be accepted. If multiple requests mutate the resources spec before
the Kubelet has accepted any of the changes, it will treat them as a single atomic request.

Note: If a second infeasible resize is made before the Kubelet allocates the first resize, there can
be a race condition where the Kubelet may or may not accept the first resize, depending on whether
it admits the first change before seeing the second. This race condition is accepted as working as
intended.

The atomic resize requirement may be reevaluated in the context of pod-level resources.

### Actuating Resizes

The resources specified by the Kubelet are not guaranteed to be the actual resources configured for
a pod or container. Examples include:
- Linux kernel enforced minimums for CPU shares & quota
- Systemd cgroup driver rounds CPU quota up to the nearest 10ms
- NRI plugins can change resource configuration

Therefore the Kubelet cannot reliably compare desired & actual resources to know whether to trigger
a resize (a level-triggered approach).

To accommodate this, the Kubelet stores the set of "actuated" resources per container.
Actuated resources represent the resource configuration that was passed to the runtime (either
via a CreateContainer or UpdateContainerResources call) and received a successful response. The
actuated resources are checkpointed alongside the allocated resources to persist across
restarts. There is the possibility that a poorly timed restart could lead to a resize request being
repeated, so `UpdateContainerResources` must be idempotent.

When a resize CRI request succeeds, the pod will be marked for resync to read the latest resources. If
the actual configured resources do not match the desired resources, this will be reflected in the
pod status resources, but not otherwise acted upon.

If a resize request does not succeed, the Kubelet will retry the resize on every subsequent pod
sync, until it succeeds or the container is terminated.

### Memory Limit Decreases

Setting the memory limit below current memory usage can cause problems. If the kernel cannot reclaim
sufficient memory, the outcome depends on the cgroups version. With cgroups v1 the change will
simply be rejected by the kernel, whereas with cgroups v2 it will trigger an oom-kill.

If the memory resize restart policy is `NotRequired` (or unspecified), the Kubelet will make a
**best-effort** attempt to prevent oom-kills when decreasing memory limits, but doesn't provide any
guarantees. Before decreasing container memory limits, the Kubelet will read the container memory
usage (via the StatsProvider). If usage is greater than the desired limit, the resize will be
skipped for that container. The pod condition `PodResizeInProgress` will remain, with an `Error`
reason, and a message reporting the current usage & desired limit. This is considered best-effort
since it is still subject to a time-of-check-time-of-use (TOCTOU) race condition where the usage exceeds the limit after the
check is performed. A similar check will also be performed at the pod level before lowering the pod
cgroup memory limit.

_Version skew note:_ Kubernetes v1.33 (and earlier) nodes only check the pod-level memory usage.

### Swap

Currently (v1.35), if swap is enabled & configured, burstable pods are allocated swap based on their
memory requests. Since resizing swap requires more thought and additional design, we will forbid
resizing memory requests of such containers for now. Since the API server is not privy to the node's
swap configuration, this will be surfaced as resizes being marked `Infeasible`.

We try to relax this restriction in the future.

### Sidecars

Sidecars, a.k.a. restartable InitContainers can be resized the same as regular containers. There are
no special considerations here. Non-restartable InitContainers cannot be resized.

### QOS Class

A pod's QOS class is immutable. This is enforced during validation, which requires that after a
resize the computed QOS Class matches the previous QOS class.

[Future enhancements: Mutable QOS Class "Shape"](#mutable-qos-class-shape) proposes a potential
change to partially relax this restriction, but is removed from the scope of this KEP.

[Future enhancements: explicit QOS Class](#design-sketch-explicit-qos-class) proposes an alternative
enhancement on that, to make QOS class explicit and improve semantics around [workload resource
resize](#design-sketch-workload-resource-resize).

### Resource Quota

With InPlacePodVerticalScaling enabled, resource quota needs to consider pending resizes. Similarly
to how this is handled by scheduling, resource quota will use the maximum of: 
1. Desired resources, computed from container requests in the pod spec, unless the resize is marked as `Infeasible`
1. Actual resources, computed from the `.status.containerStatuses[i].resources.requests`
1. Allocated resources, reported in `.status.containerStatuses[i].allocatedResources`

To properly handle scale-down, resource quota controller now needs to evaluate
pod updates where `.status...resources` changed.

### Affected Components

Pod v1 core API:
* extend API
* added validation allowing only CPU and memory resource changes

Admission Controllers: LimitRanger, ResourceQuota need to support Pod Updates:
* for ResourceQuota, podEvaluator.Handler implementation is modified to allow
  Pod updates, and verify that sum of Pod.Spec.Containers[i].Resources for all
  Pods in the Namespace don't exceed quota,
* PodResourceAllocation admission plugin is ordered before ResourceQuota.
* for LimitRanger we check that a resize request does not violate the min and
  max limits specified in LimitRange for the Pod's namespace.

Kubelet:
* set Pod's Status.ContainerStatuses[i].Resources for Containers upon placing
  a new Pod on the Node,
* update Pod's Status...AllocatedResources and Status...Resources upon resize,
* manage the new `PodResizePending` and `PodResizeInProgress` conditions
* change UpdateContainerResources CRI API to work for both Linux & Windows.

Scheduler:
* compute resource allocations using actual Status...Resources.

Other components:
* check how the change of meaning of resource requests influence other
  Kubernetes components.

### Instrumentation

The kubelet will record the following metrics:

#### `kubelet_container_requested_resizes_total`

This metric tracks the total number of resize attempts observed by the Kubelet, counted at the container level.
A single pod update changing multiple containers will be considered separate resize attempts.

Labels: 
- `resource` - what resource. Possible values: `cpu`, or `memory`. If more than one of these is changing in the resize request, we increment the counter multiple times, once for each.
- `requirement` - Possible values: `limits`, or `requests`. If more than one of these is changing in the resize request, we increment the counter multiple times, once for each.
- `operation` -  whether the resize is an increase or a decrease. Possible values: `increase`, `decrease`, `add`, or `remove`.

This metric is recorded as a counter.

#### `kubelet_pod_resize_duration_seconds`
This metric tracks the duration of [doPodResizeAction](https://github.com/kubernetes/kubernetes/blob/92de70895830ea1a9c2c6554bdab4cbee7ce867d/pkg/kubelet/kuberuntime/kuberuntime_manager.go#L699), which 
is responsible for actuating the resize.

This metric is recorded as a histogram.

#### `kubelet_pod_infeasible_resizes_total`

This metric tracks the total number of resizes that were rejected by the kubelet as infeasible.

Labels: 
- `reason_detail` - more details about why the resize is pending. Although a more detailed "message" will be provided in the `PodResizePending`
condition in the pod, we limit this label to only the following possible values to keep cardinality low:
  - `guaranteed_pod_cpu_manager_static_policy` - In-place resize is not supported for Guaranteed Pods alongside CPU Manager static policy.
  - `guaranteed_pod_memory_manager_static_policy` - In-place resize is not supported for Guaranteed Pods alongside Memory Manager static policy.
  - `static_pod` - In-place resize is not supported for static pods.
  - `swap_limitation` - In-place resize is not supported for containers with swap.
  - `insufficient_node_allocatable` - The node doesn't have enough capacity for this resize request.

This list of possible reasons may shrink or grow depending on limitations that are added or removed in the future.

This metric is recorded as a counter.

#### `kubelet_pod_pending_resizes`

This metric tracks the current count of pods that the kubelet marks as pending. This will make it
easier for us to see which of the current limitations users are running into the most.

Labels:
- `reason` - why the resize is pending. Possible values: `infeasible` or `deferred`.

This metric is recorded as a gauge.

#### `kubelet_pod_in_progress_resizes`

This metric tracks the total count of resize requests that the kubelet marks as in progress, meaning that
the resources have been allocated but not yet actuated.

This metric is recorded as a gauge.

#### `kubelet_pod_deferred_resize_accepted_total`

This metric tracks the total number of resize requests that the Kubelet originally marked as deferred but 
later accepted. This metric primarily exists because if a deferred resize is accepted through the timed retry (as
opposed to being triggered by an event such as another pod being deleted or sized down), it indicates an issue in the Kubelet's logic for handling deferred resizes that we should fix.

Labels:
  - `retry_trigger` - whether the resize was accepted through the timed retry or due to another pod event. Possible values: `periodic_retry`, `pod_resized`, `pod_updated`, `pods_added`, `pods_removed`.

This metric is recorded as a counter.

### Static CPU & Memory Policy

Resizing pods with static CPU & memory policy configured is out-of-scope for this KEP. If a pod is a guaranteed QOS on a node with a static CPU or memory policy configured, then the resize will be marked as infeasible.

This suppport will be added post-GA as a separate enhancement in its own KEP.

### Future Enhancements

1. Improve memory limit decrease oom-kill prevention by leveraging other kernel mechanisms or using
   gradual decreaese.
1. Kubelet (or Scheduler) evicts lower priority Pods from Node to make room for
   resize. Pre-emption by Kubelet may be simpler and offer lower latencies.
1. Allow ResizePolicy to be set on Pod level, acting as default if (some of)
   the Containers do not have it set on their own.
1. Extend ResizePolicy to separately control resource increase and decrease
   (e.g. a Container can be given more memory in-place but decreasing memory
   requires Container restart).
1. Handle resize of guaranteed pods with static CPU or memory policy.
1. Extend controllers (Job, Deployment, etc) to propagate Template resources
   update to running Pods.
1. Allow resizing local ephemeral storage.
1. Handle pod-scoped resources (https://github.com/kubernetes/enhancements/pull/1592)
1. Explore periodic resyncing of resources. That is, periodically issue resize requests to the
   runtime even if the allocated resources haven't changed.
1. Allow resizing containers with swap allocated.

#### Mutable QOS Class "Shape"

This change was originally proposed for Beta, but moved out of the scope. It may still be considered
for a future enhancement to relax the constraints on resizes.

A pod's QOS class **cannot be changed** once the pod is started, independent of any resizes.

To clarify the discussion of the proposed QOS Class changes, the following terms are defined:

* "QOS Class" - The QOS class that was computed based on the original resource requests & limits
  when the pod was first created.
* "QOS Shape" - The QOS class that _would_ be computed based on the current resource requests &
  limits.

On creation, the QOS Class is equal to the QOS Shape. After a resize, the QOS Shape must be greater
than or equal to the original QOS Class:

* Guaranteed pods: must maintain `requests == limits`, and must be set for both CPU & memory
* Burstable pods: _can_ be resized such that `requests == limits`, but their original QOS
class will stay burstable. Must retain at least one CPU or memory request or limit.
* BestEffort pods: can be freely resized, but stay BestEffort.

Even though the QOS Shape is allowed to change, the original QOS class is used for all
decisions based on QOS class:

* `.status.qosClass` always reports the original QOS class
* Pod cgroup hierarchy is static, using the original QOS class
* Non-guaranteed pods remain ineligible for guaranteed CPUs or NUMA pinning
* Preemption uses the original QOS Class
* OOMScoreAdjust is calculated with the original QOS Class
* Memory pressure eviction is unaffected (doesn't consider QOS Class)

The original QOS Class is persisted to the status. On restart, the Kubelet is allowed to read the
QOS class back from the status.

See [future enhancements: explicit QOS Class](#design-sketch-explicit-qos-class) for a possible
change to make QOS class explicit and improve semantics around
[workload resource resize](#design-sketch-workload-resource-resize).

#### Design Sketch: Workload resource resize

The following [workload resources](https://kubernetes.io/docs/concepts/workloads/) are considered
for in-place resize support:

* Deployment
* ReplicaSet
* StatefulSet
* DaemonSet
* Job
* CronJob

Each of these resources will have a new `ResizePolicy` field added to the spec. In the case of
Deployments or Cronjobs, the child (ReplicaSet/Job) will inherit the policy. The resize policy is
set to one of: `InPlace` or `Recreate` (default). If the policy is set to recreate, the behavior is
unchanged, and generally induces a rolling update.

If the policy is set to in-place, the controller will *attempt* to issue an in-place resize to all
the child pods. If the resize is not a legal in-place resize, such as changing from guaranteed to
burstable, the replicas will be recreated.

Open Questions:
* Will resizes be issued through a new `/resize` subresource? If so, what happens if a resize is
  made that doesn't go through the subresource?
* Does ResizePolicy need to be per-resource type (similar to the resize restart policy on pods)?
* Can you do a rolling-in-place-resize, or are all child pod resizes issued more or less
  simultaneously?

#### Design Sketch: Explicit QOS Class

Workload resource resize presents a problem for QOS handling. For example:

1. ReplicaSet created with a burstable pod shape
2. Initial burstable replicas created
3. Resize to a guaranteed shape
4. Initial replicas are still burstable, but with a guaranteed shape
5. Horizontally scale the RS to add additional replicas
6. New replicas are created with the guaranteed resource shape, and assigned the guaranteed QOS class
7. Resize back to a burstable shape (undoing step 3)

After step 6, there are a mix of burstable & guaranteed replicas. In step 7, the burstable pods can
be resized in-place, but the guaranteed pods will need to be recreated.

To mitigate this, we can introduce an explicit QOSClass field to the pod spec. If set, it must be
less than or equal to the QOS shape. In other words, you can set a guaranteed resource shape but an
explicit QOSClass of burstable, but not the other way around. If set, the status QOSClass is synced
to the explicit QOSClass, and the rest of the behavior is unchanged from the
[QOS Class Proposal](#qos-class).

Going back to the earlier example, if the original ReplicaSet set an explicit Burstable QOSClass,
then the heterogeneity in step 6 is avoided. Alternatively, if there was a real desire to switch to
guaranteed in step 3, then the explicit QOSClass can be changed, triggering a recreation of all
replicas.

#### Design Sktech: Pod-level Resources

Adding resize capabilities to [Pod-level Resources](https://github.com/kubernetes/enhancements/issues/2837)
should largely mirror container-level resize. This includes:
- Add actual resources to `PodStatus.Resources`
- Track allocated pod-level resources
- Factor pod-level resource resize into ResizeStatus logic
- Pod-level resizes are treated as atomic with container level resizes.

Open questions:
- Details around defaulting logic, pending finalization in the pod-level resources KEP
- If the resize policy is `RestartContainer`, are all containers restarted on pod-level resize? Or
  does it depend on whether container-level cgroups are changing?
