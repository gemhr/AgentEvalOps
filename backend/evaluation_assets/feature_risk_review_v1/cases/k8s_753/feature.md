## Summary

<!--
This section is incredibly important for producing high-quality, user-focused
documentation such as release notes or a development roadmap. It should be
possible to collect this information before implementation begins, in order to
avoid requiring implementors to split their attention between writing release
notes and implementing the feature itself. KEP editors and SIG Docs
should help to ensure that the tone and content of the `Summary` section is
useful for a wide audience.

A good summary is probably at least a paragraph in length.

Both in this section and below, follow the guidelines of the [documentation
style guide]. In particular, wrap lines to a reasonable length, to make it
easier for reviewers to cite specific portions, and to minimize diff churn on
updates.

[documentation style guide]: https://github.com/kubernetes/community/blob/master/contributors/guide/style-guide.md
-->
Sidecar containers are a new type of containers that start among the Init
containers, run through the lifecycle of the Pod and don’t block pod
termination. Kubelet makes a best effort to keep them alive and running while
other containers are running.

## Motivation

<!--
This section is for explicitly listing the motivation, goals, and non-goals of
this KEP.  Describe why the change is important and the benefits to users. The
motivation section can optionally provide links to [experience reports] to
demonstrate the interest in a KEP within the wider Kubernetes community.

[experience reports]: https://github.com/golang/go/wiki/ExperienceReports
-->
The concept of sidecar containers has been around since the early days of
Kubernetes. A clear example is [this Kubernetes blog post](https://kubernetes.io/blog/2015/06/the-distributed-system-toolkit-patterns/#example-1-sidecar-containers)
from 2015 mentioning the sidecar pattern.

Over the years the sidecar pattern has become more common in applications,
gained popularity and the use cases are getting more diverse. The current
Kubernetes primitives handle that well, but they fall short for
several use cases and force weird work-arounds in the applications.

The next sections expand on what the current problems are. But, to give more
context, it is important to highlight that some companies are already using a
fork of Kubernetes with this sidecar functionality added (not all
implementations are the same, but more than one company has a fork for this).

### Problems: jobs with sidecar containers

Imagine you have a Job with two containers: one which does the main processing
of the job and the other is just a sidecar facilitating it. This sidecar could
be a service mesh, a metrics gathering statsd server, etc.

When the main processing finishes, the pod won't terminate until the sidecar
container finishes too. This is problematic for sidecar containers that run
continuously.

There is no simple way to handle this on Kubernetes today. There are
work-arounds for this problem, most of them consist of some form of coupling
between the containers to add some logic where a container that finishes
communicates it so other containers can react. But it gets tricky when you have
more than one sidecar container or want to auto-inject sidecars.

The sidecar will also not be restarted for jobs with `restartPolicy:Never` when
it was OOM killed, which may render the pod unusable if the sidecar provided
secure communication to other containers. The issue gets complicated by the fact
that sidecar containers typically have smaller request, making them the first
target as OOM score adjustment uses the request as a main input for calculation.

### Problems: log forwarding and metrics sidecar

A log forwarding sidecar should start before several other containers, to simplify
getting logs from the startup of other applications and from the Init
containers. Let's call "main" container the app that will log and "logging"
container the sidecar that will facilitate it.

If the logging container starts after the main app, special logic needs to be
implemented to gather logs from the main app. Furthermore, if the logging
container is not yet started and the main app crashes on startup, those logs are
more likely to be lost (depends if logs go to a shared volume or over the
network on localhost, etc.). While you can modify your application to handle
this scenario during startup (as it is probably the change you need to do to
handle sidecar crashes), for shutdown this approach won't work.

On shutdown the ordering behavior is arguably more important: if the logging
container is stopped first, logs for other containers are lost. No matter if
those containers queue them and retry to send them to the logging container, or
if they are persisted to a shared volume. The logging container is already
killed and will not be restarted, as the pod is shutting down. In these cases,
logs are lost.

The same things regarding startup and shutdown apply for a metrics container.

### Problems: service mesh

Service mesh presents a similar problem: you want the service mesh container to
be running and ready before other containers start, so that any inbound/outbound
connections that a container can initiate go through the service mesh.

A similar problem happens for shutdown: if the service mesh container is
terminated prior to the other containers, outgoing traffic from other apps will
be blackholed or not use the service mesh.

However, as none of these are possible to guarantee, most service meshes (like
Linkerd and Istio), use fragile and platform specific workarounds to have the basic functionality.
For example, for termination, projects like
[kubeexit](https://github.com/karlkfi/kubexit) or [custom solutions](https://suraj.io/post/how-to-gracefully-kill-kubernetes-jobs-with-a-sidecar/)
are used. 

Some service meshes depend on secret (like a certificate) downloaded by other
init container to establish secure communication between services. This makes
the problem of ordering of sidecar and init containers harder.

Another complication is between log forwarding and service mesh sidecars.
Service mesh sidecars would provide the networking while log forwarding needs to
be active to upload logs. Startup and tear down sequence of those may be a
complicated problem.

### Problems: configuration / secrets

Some pods use init containers to pull down configuration/secrets and update them
before the main container gets it. Then use sidecars to continue to watch for
changes and perform the updates and push to the main container. This requires
two separate code paths today. Perhaps the same sidecar could be used for both
cases.

### Goals

<!--
List the specific goals of the KEP. What is it trying to achieve? How will we
know that this has succeeded?
-->
This proposal aims to:
- make containers implementing the sidecar pattern first class citizens inside a
  Pod
- solve a Job completion issue when sidecars should run continuously
- allow mixing initContainers and sidecars for a choreographed startup sequence
- allow easy injection of sidecar containers in any Pod
- (to be evaluated after alpha) allow to implement sidecar containers that will
  guaranteed to be running longer than regular containers 

### Non-Goals

<!--
What is out of scope for this KEP? Listing non-goals helps to focus discussion
and make progress.
-->
This proposal doesn't aim to:
- support arbitrary dependencies graphs between containers
- act as a security control to enforce that pod containers only run while the
  sidecar is healthy. Restart of sidecar containers is a best effort
- allow to enforce security boundaries to sidecar containers different that
  other containers. For example, allow Istio to run privileged to configure ip
  tables and disable other containers from doing so
- (alpha) support containers termination ordering

## Proposal

<!--
This is where we get down to the specifics of what the proposal actually is.
This should have enough detail that reviewers can understand exactly what
you're proposing, but should not include things like API designs or
implementation. What is the desired outcome and how do we measure success?.
The "Design Details" section below is for the real
nitty-gritty.
-->
The proposal is to introduce a `restartPolicy` field to init containers and use
it to indicate that an init container is a sidecar container. Kubelet will start
init containers with `restartPolicy=Always` in the order with other init
containers, but instead of waiting for its completion, it will wait for the
container startup completion.

The condition for startup completion will be that the startup probe succeeded
(or if no startup probe defined) and `postStart` handler completed. This
condition is represented with the field `Started` of `ContainerStatus` type. See
the section ["Pod startup completed condition"](#pod-startup-completed-condition)
for considerations on picking this signal.

The field `restartPolicy` will only be accepted on init
containers as part of this KEP. The only supported value proposed in this KEP is `Always`. No other
values will be defined as part of this KEP. Moreover, the field will be
nullable so the default value will be "no value". 

Other values for `restartPolicy` of containers will not be accepted and
containers will follow the logic is currently implemented (documented
[here](https://kubernetes.io/docs/concepts/workloads/pods/init-containers/#:~:text=if%20a%20pod's%20init%20container%20fails%2C%20the%20kubelet%20repeatedly%20restarts%20that%20init%20container%20until%20it%20succeeds.%20however%2C%20if%20the%20pod%20has%20a%20restartpolicy%20of%20never%2C%20and%20an%20init%20container%20fails%20during%20startup%20of%20that%20pod%2C%20kubernetes%20treats%20the%20overall%20pod%20as%20failed.)
and more details can be found in the section
["Future use of restartPolicy field"](#future-use-of-restartpolicy-field)).

Sidecar containers will not block Pod completion - if all regular containers
complete, sidecar containers will be terminated.

The `restartPolicy` field for individual init containers can override the
Pod-level `restartPolicy` for sidecar containers. As a result, even if the Pod's
`restartPolicy` is set to `Never` or `OnFailure`, sidecar containers will still
be restarted.

Note, a separate KEP https://github.com/kubernetes/enhancements/issues/4438 will enable
sidecar containers to be restarted even during Pod termination.

In order to minimize OOM kills of sidecar containers, the OOM adjustment for
these containers will match or fall below the OOM score adjustment of regular
containers in the Pod. This intent to solve the issue
https://github.com/kubernetes/kubernetes/issues/111356 

As part of this KEP we also will be enabling for sidecar containers (those will
not be allowed for other init containers):
- `PostStart` and `PreStop` lifecycle handlers for sidecar containers
- All probes (startup, readiness, liveness)
- Readiness probes of sidecars will contribute to determine the whole Pod
  readiness.

```yaml
kind: Pod
spec:
  initContainers:
  - name: vault-agent
    image: hashicorp/vault:1.12.1
  - name: istio-proxy
    image: istio/proxyv2:1.16.0
    args: ["proxy", "sidecar"]
    restartPolicy: Always
  containers:
  ...
```

### Naming

This section explains the motivation for naming, assuming that sidecar
containers and init containers belong to the same collection and distinguished
using a field. Other alternatives are outlined in other section.

#### Collection name

For this KEP it is important to have sidecar containers be defined among other
init containers to be able to express the initialization order of containers.
The name `initContainers` is not a good fit for sidecar containers as they
typically do more than initialization. The better name can be “infrastructure”
containers. The current idea is to implement sidecars as a part of
`initContainers` and if this introduces too much trouble, the new collection
name may replace the old collection name in future.

*Alternative* is to introduce a new collection `infrastructureContainers` that
replaces semantically `initContainers` and deprecate the `initContainers`. This
collection will allow both - init containers and sidecar containers. Decision on
this alternative can be postponed to after alpha implementation.

Another *alternative* is to instead of containers, insert placeholders to the
`initContainers` collection. Containers themselves are defined in other
collection. This option will likely confuse end users more than will help.

#### Reuse of `restartPolicy` field and enum

The per-container restart policy was a long standing request from the community.
Implementing per-container restart policy introduces a set of challenging
problems for the pod lifecycle. For example, the state keeping for containers
which has already run to completion. Introducing sidecar containers is another
scenario where this field can be semantically used.

Introducing this field on containers opens up opportunities to implement those
long-standing requests from the community.

*Alternative* is to introduce a new field: `ambient: true` on all containers.
This property will make containers be restarted all the time, and will not block
the pod completion.
  - Pros: detaching sidecar proposal from the per-container `restartPolicy`
    proposal
  - Cons: this field is a new concept that will be introduced for the same
    property that is typically controlled by `restartPolicy`.

#### Use `Always` vs. New enum value

The semantic of an `Always` enum value is very close to what sidecars require.
This is why reusing Always to represent sidecars makes a lot of sense for Init
containers.

There are a few pros and cons to reuse `Always` as the value instead of
introducing a new enum value
`UntilPodTermination`/`UntilPodShutdown`/`WithPod`/`Ambient` for `restartPolicy`
on containers. 

Pros:
- Allows the same enum values to be used for the `restartPolicy` of both pods
  and containers, but semantics that are mostly the same.
- Less 
  
Cons:
- There are slight differences between the semantics of `Always` for containers
  and `Always` for pods. The main difference is that a `initContainer` with
  `Always` will be terminated when the pod terminates and has no influence over
  the pod lifecycle. Also, for Pods, the default `restartPolicy` is
  [documented](https://github.com/kubernetes/kubernetes/blob/280473ebc4e45f9001f5f9789c318ff7329bc5f0/staging/src/k8s.io/api/core/v1/types.go#L2753-L2764)
  as `Always` but for `initContainers` it will default to `OnFailure`. We
  believe this can be addressed in the documentation of the existing enum
  fields.

When in future we may support `Always` on regular containers, there will be
interesting case of `Always` having a meaning of non-blocking container for the
Pod with `restartPolicy == Never` or `OnFailure`. This is easy to explain - Pod
lifecycle is controlled by containers with the matching restartPolicy. But it
may be slightly confusing and needs to be carefully reviewed at the time this
feature will be considered.

## Design Details

<!--
This section should contain enough information that the specifics of your
change are understandable. This may include API specs (though not always
required) or even code snippets. If there's any ambiguity about HOW your
proposal will be implemented, this is the place to discuss them.
-->
### Backward compatibility

The new field means that any Pod that is not using this field will behave the
same way as before.

The new field will only work with the proper control plane and kubelet. Upgrade
and downgrade scenarios will be covered in the further sections.

Outside of Kubernetes-controlled code, there might be third party controllers or
existing containers relying on the current behavior of init containers.
Behaviors they can rely on:

- Assuming nothing is running in the Pod when init container starts. For
  instance taking PID of the process assuming there are no sidecars.
- Incorrectly calculating the resource usage of a Pod not taking the new type of
  containers into account (e.g. some grafana dashboards may need modification)
- Stripping the restartPolicy:Always from init container as an unknown field
  rendering Pod unfunctional as it will not pass the init stage after this
- OPA rules may reject the Pod with new unknown fields because of failure to
  parse the new field

These potential incompatibilities will be documented.

### kubectl changes

The `kubectl get pods` filters all the Init containers from output when Pod is Running.
As part of this KEP, the output will be extended to include status of sidecar Containers.
#### Without sidecar containers support

For the Pod:

```
initContainers:
  - name: init-config
containers:
  - name: sidecar-1
  - name: sidecar-2
  - name: main
```

Initialization (Waiting)

```
NAME      READY   STATUS     RESTARTS   AGE
test      0/3     Init:0/1   0          0s
```
Running

```
NAME      READY   STATUS     RESTARTS   AGE
test      3/3     Running    0          35s
```

#### With sidecar container feature

For the Pod:

```
initContainers:
  - name: init-config
  - name: sidecar-1
    restartPolicy: Always
  - name: sidecar-2
    restartPolicy: Always
containers:
  - name: main
```

What we have today:

Initialization (Waiting)

```
NAME      READY   STATUS     RESTARTS   AGE
test      0/1     Init:0/3   0          0s
NAME      READY   STATUS     RESTARTS   AGE
test      0/1     Init:1/3   0          5s
NAME      READY   STATUS     RESTARTS   AGE
test      0/1     Init:2/3   0          10s
```

Running

```
NAME      READY   STATUS     RESTARTS   AGE
test      1/1     Running    0          35s
```

What will be returned as part of the KEP implementation:

Initialization (Waiting)

```
NAME      READY   STATUS     RESTARTS   AGE
test      0/3     Init:0/3   0          0s
NAME      READY   STATUS     RESTARTS   AGE
test      0/3     Init:1/3   0          5s
NAME      READY   STATUS     RESTARTS   AGE
test      0/3     Init:2/3   0          10s
```

Running

```
NAME      READY   STATUS     RESTARTS   AGE
test      3/3     Running    0          35s
```

### Resources calculation for scheduling and pod admission

When calculating whether Pod will fit the Node, resource limits and requests are
being calculated.

Resources calculation will change for Pod with sidecar containers. Today
resources are calculated as a maximum between the maximum use of an
InitContainer and Sum of all regular containers:

`Max ( Max(initContainers), Sum(Containers) )`

With the sidecar containers the formula will change.

Easiest formula would be to assume they are running the duration of the init
stage as well as regular containers.

`Max ( Max(nonSidecarInitContainers) + Sum(Sidecar Containers), Sum(Sidecar
Containers) + Sum(Containers) )`

However the true calculations will be different as all init containers that
completed before the first sidecar containers will not need to account for any
sidecar containers for the maximum value calculation.

So the formula, assuming the function:

```
InitContainerUse(i) = Sum(sidecar containers with index < i) + InitContainer(i)
```

Is this:

```
Max ( Max( each InitContainerUse ) , Sum(Sidecar Containers) + Sum(Containers) ) 
```

There is also a [Pod overhead](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-overhead/)
that is being added to the resource usage. This section assumes it will be added
by kubelet independently from this formulae computation.

### Exposing Pod Resource requirements

It’s currently not straightforward for users to know the effective resource
requirements for a pod. The formula for this is:

`Max ( Max(initContainers), Sum(Containers)) + pod overhead`.

This is derived from the fact that init containers run serially and to
completion before non-init containers.  The effective request for each resource
is then the maximum of the largest request for a resource in any init container,
and the sum of that resources across all non-init containers.

The introduction of in place pod updates of resource requirement in
[KEP#1287](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/1287-in-place-update-pod-resources)
further complicates effective resource requirement calculation as
`Pod.Spec.Containers[i].Resources` becomes a desired state field and may not
represent the actual resources in use.  The KEP notes that:

> Schedulers should use the larger of `Spec.Containers[i].Resources` and
> `Status.ContainerStatuses[i].ResourcesAllocated` when considering available
> space on a node.

We will introduce `ContainerUse` to represent this value:

```
ContainerUse(i) = Max(Spec.Containers[i].Resources, Status.ContainerStatuses[i].ResourcesAllocated)
```

In the absence of KEP 1287, or if the feature is disabled, `ContainerUse` is
simply:

```
ContainerUse(i) = Spec.Containers[i].Resources
```

The sidecar KEP also changes that calculation to be more complicated as sidecar
containers are init containers that do not terminate.  Since init containers
start in order, sidecar resource usage needs to be summed into those init
containers that start after the sidecar.  Defining `InitContainerUse` as:

```
InitContainerUse(i) = Sum(sidecar containers with index < i) + Max(Spec.InitContainers[i].Resources, Status.InitContainerStatuses[i].ResourcesAllocated)
```

allows representing the new formula for a pods resource usage

```
Max ( Max( each InitContainerUse ) , Sum(Sidecar Containers) + Sum(each ContainerUse) ) + pod overhead
```

Even now, users still sometimes find how a pod's effective resource requirements
are calculated confusing or are just unaware of the formula.  The mitigating
quality to this is that init container resource requests are usually lower than
the sum of non-init container resource requests, and can be ignored by users in
those cases.  Software that requires accurate pod resource requirement
information (e.g. kube-scheduler, kubelet, autoscalers) don't have that luxury.
It is too much to ask of users to perform this even more complex calculation
simply to know the amount of free capacity they need for a given resource to
allow a pod to schedule.

This KEP will not expose the total resource requests field to end user
as many decisions on this field need to be made from other KEPs: InPlace pod update 
and Pod Level resources. We do not want to make it harder for those new KEPs
to be implemented by exposing this field prematurely.

#### Resources calculation and Pod QoS evaluation

Sidecar containers will be used for Pod QoS calculation as all other containers.

The logic in
[`GetPodQOS`](https://github.com/kubernetes/kubernetes/blob/release-1.26/pkg/apis/core/helper/qos/qos.go#L38-L101)
not likely will need changes, but needs to be tested with the sidecar
containers.

### Topology and CPU managers

[NodeResourcesFit scheduler plugin](https://github.com/kubernetes/kubernetes/blob/release-1.26/pkg/scheduler/framework/plugins/noderesources/fit.go#L160-L176)
will need to be updated take sidecar container resource request into
consideration.

Preliminary code analysis didn't expose any issues introducing sidecar
containers. The biggest question is resources reuse for sidecar containers and
other init containers, especially in cases of single NUMA node requirements and
such. This may be non-trivial. The decision on this is not blocking the KEP
though.

From the code, it appears that init containers are treated exactly like
application containers so we don't need to change anything from resource
management point of view. I found references in the code where all the
containers (init containers and application containers) were coalesced before
resources (CPUs, memory and devices) are allocated to them. Here are a few
examples:

- Container Manager:
  https://github.com/kubernetes/kubernetes/blob/release-1.26/pkg/kubelet/cm/container_manager_linux.go#L708
- CPU Manager:
  https://github.com/kubernetes/kubernetes/blob/release-1.26/pkg/kubelet/cm/cpumanager/policy_static.go#L490
- Memory Manager:
  https://github.com/kubernetes/kubernetes/blob/release-1.26/pkg/kubelet/cm/memorymanager/policy_static.go#L372
- Topology Manager:
  - https://github.com/kubernetes/kubernetes/blob/release-1.26/pkg/kubelet/cm/topologymanager/scope.go#L137
  - https://github.com/kubernetes/kubernetes/blob/release-1.26/pkg/kubelet/cm/topologymanager/scope_container.go#L52
  - https://github.com/kubernetes/kubernetes/blob/release-1.26/pkg/kubelet/cm/topologymanager/scope_pod.go#L58


### Termination of containers

In Alpha sidecar containers will be terminated as regular containers. No special
or additional signals will be supported.

In Beta we have thought about introducing additional termination grace period fields
to manage termination duration
([draft proposal](https://docs.google.com/document/d/1B01EdgWJAfkT3l6CIwNwoskQ71eyuwet3mjiQrMQbU8))
and leverage these fields to add reverse order termination of sidecar containers
after the primary containers terminate.

However, we decided on an alternative that doesn't require additional fields or hooks while keeping
the desired behaviors when Pods with sidecars are terminated. While original approach works better
with truly graceful termination where consistency is more important than time taken, proposed approach
works for that scenario as well as a more and more popular scenario of limited time to terminate when
graceful termination is set by external requirement and Pods needs to do best to gracefully terminate
as much as possible (think of a Spot Instances with 30 seconds notification).

Here is the proposed approach:
1. Sidecar containers that have a `PreStop` hook will be notified when the Pod has begun terminating
   by executing the `PreStop` hook. This happens at the same time as regular containers, and begins
   the Pod's termination grace period countdown.
2. Once the last primary container terminates, the last started sidecar container is notified by
   sending a `SIGTERM` signal.
3. The next sidecar (in reverse order) is notified by sending a `SIGTERM` signal after the previous
   sidecar container terminates.
4. This continues until all sidecar containers have terminated, or the Pod's termination grace period
   expires.
5. In the latter case, all remaining containers are notified by a `SIGTERM`, followed by a fixed
   grace period of 2s and finally terminated.
6. The Pod will be terminated after that.

Pseudocode for the above:

```
func terminatePod() {
  // notify all sidecar containers with preStop hook, asynchronously
  for sidecar in sidecarContainers {
    if sidecar has preStop hook {
      go execute preStop hook // async
    }
  }
  // notify all containers with preStop hook and then SIGTERM, asynchronously
  for container in containers {
    if container has preStop hook {
      go func(container) { // async
        execute preStop hook
        send SIGTERM
      }
    }
  }
  for {
    switch {
      case grace period expired:
        for anyContainer in sidecarContainers + containers {
          if anyContainer is running {
            send SIGTERM
          }
        }
        sleep 2s
        for anyContainer in sidecarContainer + containers {
          if anyContainer is running {
            send SIGKILL
          }
        }
        return
      case all containers are terminated:
        // sidecars are terminated in reverse order
        for sidecar in reverse(sidecarContainers) {
          // sidecar is already terminating, let it finish
          if sidecar is terminating {
            break
          }
          // next sidecar to terminate
          else if sidecar is running {
            send SIGTERM
            break
          }
        }
        sleep 1s
      case all sidecarContainers are terminated:
        return
      default:
        sleep 1s
    }
  }
}
```

It is worth noting that, like with regular containers, `PreStop` hook must complete before the `SIGTERM`
signal to stop the sidecar container can be sent. Therefore, ordering and graceful termination of sidecars
can only be guaranteed if the `PreStop` hook completes within the Pod's termination grace period.

Sidecars continue to be restarted until they enter the `Terminated` state which they are notified
by a `SIGTERM` signal. This is to ensure that sidecars that fail are restarted until the TGPS expires.

We might postpone running the `livenessProbe` for restarted sidecar containers during termination
until GA, depending on the implementation complexity.

If we compare this to the initial proposal, the following behaviors are preserved:
- Sidecars should not begin termination until all primary containers have
  terminated.
  - Implicit in this is that sidecars should continue to be restarted until all
    primary containers have terminated.
- Sidecars should terminate serially and in reverse order. I.e. the first
  sidecar to initialize should be the last sidecar to terminate.

The additional benefits of this approach comparing to initial proposal:
- If graceful termination period is short, and mostly taken by the main container, the sidecar containers
  has more time to gracefully terminate, for example, clear up buffers of logging container.
- There is absolutely no change in behavior of main containers - they start graceful termination at exact
  same time as before and can utilize as much of the graceful termination period as they need. The Pod graceful
  termination period semantic also stay unchanged.

### Other

This behavior needs to be adjusted:
https://github.com/kubernetes/enhancements/issues/3676
