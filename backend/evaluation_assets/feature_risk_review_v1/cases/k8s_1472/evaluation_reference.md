### Test Plan

The Kubernetes scheduler extension will be tested with new unit tests
that simulate a variety of scenarios:
- different volume sizes and types
- driver with and without storage capacity tracking enabled
- capacity information for node local storage (node selector with one
  host name), network attached storage (more complex node selector),
  storage available in the entire cluster (no node restriction)
- no suitable node, one suitable node, several suitable nodes

Producing capacity information in external-provisioner also can be
tested with new unit tests. This has to cover:
- different modes
- different storage classes
- a driver response where storage classes matter and where they
  don't matter
- different topologies
- various older capacity information, including:
  - no entries
  - obsolete entries
  - entries that need to be updated
  - entries that can be left unchanged

This needs to run with mocked CSI driver and API server interfaces to
provide the input and capture the output.

Full end-to-end testing is needed to ensure that new RBAC rules are
identified and documented properly. For this, a new alpha deployment
in csi-driver-host-path is needed because we have to make changes to
the deployment like setting `CSIDriver.spec.storageCapacity` which
will only be valid when tested with Kubernetes clusters where
alpha features are enabled.

The CSI hostpath driver needs to be changed such that it reports the
remaining capacity of the filesystem where it creates volumes. The
existing raw block volume tests then can be used to ensure that pod
scheduling works:
- Those volumes have a size set.
- Late binding is enabled for the CSI hostpath driver.

A new test can be written which checks for `CSIStorageCapacity` objects,
asks for pod scheduling with a volume that is too large, and then
checks for events that describe the problem.

### Graduation Criteria

#### Alpha -> Beta Graduation

- Gather feedback from developers and users
- Evaluate and where necessary, address [drawbacks](#drawbacks)
- Extra CSI API call for identifying storage topology, if needed
- Revise ownership of `CSIStorageCapacity` objects:
  - some drivers run outside the cluster and thus cannot own them
  - with pods as owner of per-node objects, upgrading the driver
    will cause all objects to be deleted and recreated by the
    updated driver
- Re-evaluate API choices, considering:
  - performance
  - extensions of the API that may or may not be needed (like
    [ignoring storage class
    parameters](#storage-class-parameters-that-never-affect-capacity))
  - [advanced storage placement](https://github.com/kubernetes/enhancements/pull/1347)
- Tests are in Testgrid and linked in KEP

#### Beta -> GA Graduation

- 5 CSI drivers enabling the creation of `CSIStorageCapacity` data
- 5 installs
- More rigorous forms of testing e.g., downgrade tests and scalability tests
- Allowing time for feedback
- Design for support in [Cluster Autoscaler](https://github.com/kubernetes/autoscaler)

### Upgrade / Downgrade Strategy

<!--
If applicable, how will the component be upgraded and downgraded? Make sure
this is in the test plan.

Consider the following in developing an upgrade/downgrade strategy for this
enhancement:
- What changes (in invocations, configurations, API use, etc.) is an existing
  cluster required to make on upgrade in order to keep previous behavior?
- What changes (in invocations, configurations, API use, etc.) is an existing
  cluster required to make on upgrade in order to make use of the enhancement?
-->

### Version Skew Strategy

<!--
If applicable, how will the component handle version skew with other
components? What are the guarantees? Make sure this is in the test plan.

Consider the following in developing a version skew strategy for this
enhancement:
- Does this enhancement involve coordinating behavior in the control plane and
  in the kubelet? How does an n-2 kubelet without this feature available behave
  when this feature is used?
- Will any other components on the node change? For example, changes to CSI,
  CRI or CNI may require updating that component before the kubelet.
-->

## Production Readiness Review Questionnaire

### Feature enablement and rollback

* **How can this feature be enabled / disabled in a live cluster?**
  - [X] Feature gate
    - Feature gate name: CSIStorageCapacity
    - Components depending on the feature gate:
      - apiserver
      - kube-scheduler
  - [X] CSIDriver.StorageCapacity field can be modified
    - Components depending on the field:
      - kube-scheduler

* **Does enabling the feature change any default behavior?**

  Enabling it only in kube-scheduler and api-server by updating
  to a Kubernetes version where it is enabled and not in any of the
  running CSI drivers causes no changes. Everything continues as
  before because no `CSIStorageCapacity` objects are created and
  kube-scheduler does not wait for any.

  That changes once the feature is enabled in a CSI driver. Then pod
  scheduling becomes more likely to pick suitable nodes. This happens
  automatically, without having to change application deployments.

* **Can the feature be disabled once it has been enabled (i.e. can we rollback
  the enablement)?**

  Yes, by disabling it in the CSI driver deployment:
  `CSIDriver.StorageCapacity=false` causes kube-scheduler to ignore storage
  capacity for the driver. In addition, external-provisioner can be deployed so
  that it does not publish capacity information (`--enable-capacity=false`).

  Downgrading to a previous Kubernetes release may also disable the feature or
  allow disabling it via a feature gate: In Kubernetes 1.19 and 1.20,
  registration of the `CSIStorageCapacity` type was controlled by the feature
  gate. In 1.21, the type will always be enabled in the v1beta1 API group. In
  1.24, the type is always enabled in the v1 API unconditionally.

  Depending on the combination of Kubernetes release and
  feature gate, the type will be disabled. However, any existing
  objects will still remain in the etcd database, they just won't be
  visible.

  When the type is disabled, external-provisioner will be unable to update
  objects: this needs to be treated with exponential backoff just like other
  communication issues with the API server.

  The new flag in `CSIDriver` will be preserved when disabling the
  feature gate in the apiserver. kube-scheduler
  will continue to do scheduling with capacity information until it
  gets rolled back to a version without support for that or the feature
  is turned off for kube-scheduler.

  The new flag is not preserved when rolling back to a release older
  than 1.19 where the flag did not exist yet.

* **What happens if we reenable the feature if it was previously rolled back?**

  Stale objects will either get garbage collected via their ownership relationship
  or get updated by external-provisioner. Scheduling with capacity information
  resumes.

* **Are there any tests for feature enablement/disablement?**
  The e2e framework does not currently support enabling and disabling feature
  gates. However, unit tests in each component dealing with managing data created
  with and without the feature are necessary and were added before
  before the transition to beta, for example
  [in the apiserver](https://github.com/kubernetes/kubernetes/blob/v1.21.0/pkg/apis/storage/validation/validation_test.go#L2091-L2131)
  and the [volume binder](https://github.com/kubernetes/kubernetes/blob/v1.21.0/test/integration/volumescheduling/volume_binding_test.go#L706-L709).

### Rollout, Upgrade and Rollback Planning

* **How can a rollout fail? Can it impact already running workloads?**

A rollout happens in at least two phases:
1. Updating the cluster so that the `CSIStorageCapacity` API is enabled in the apiserver
   and the kube-scheduler uses that information *for drivers which have opted into this*.
2. CSI driver installations get updated such that they produce `CSIStorageCapacity` objects
   and enable usage of those objects in their `CSIDriver` object.

In the first phase, scheduling of pods should continue as before
because no CSI driver has opted into the feature yet. If it doesn't
continue, then the implementation is faulty and the feature needs to
be disabled again until a fix is available. Then second phase gets
skipped and the cluster operates as before.

If the second phase fails because a driver malfunctions or overloads
the apiserver, then it can be rolled back and scheduling again happens
without using storage capacity information.

In none of these cases are running workloads affected unless support
for the new API is broken such that the apiserver is affected.
Fundamental bugs may cause unexpected apiserver shutdowns or show up
as 5xx error codes for operations involving `CSIStorageCapacity`
objects.

* **What specific metrics should inform a rollback?**

One is an increased number of pods that are not getting scheduled with
events that quote `node(s) did not have enough free storage` as reason
when the cluster is not really running out of storage capacity.

Another is a degradation in apiserver metrics (increased CPU or memory
consumption, increased latency), specifically
[`apiserver_request_duration_seconds`](https://github.com/kubernetes/kubernetes/blob/645c40fcf6f1fca133a00c8186674bcbcecc4b8e/staging/src/k8s.io/apiserver/pkg/endpoints/metrics/metrics.go#L98).

* **Were upgrade and rollback tested? Was upgrade->downgrade->upgrade path tested?**

This was done manually before transition to beta in a kubeadm-based cluster
running on VMs. The experiment confirmed that rollback and re-enabling works
as described above, with no unexpected behavior.

* **Is the rollout accompanied by any deprecations and/or removals of features,
  APIs, fields of API types, flags, etc.?**

No.

### Monitoring requirements

* **How can an operator determine if the feature is in use by workloads?**

The feature itself is not used by workloads. It is used when
scheduling workloads onto nodes, but not while those run.

That a CSI driver provides storage capacity information can seen in the
following metric data that will be provided by external-provisioner instances:
- total number of `CSIStorageCapacity` objects that the external-provisioner
  is currently meant to manage for the driver: `csistoragecapacities_desired_goal`
- number of such objects that currently exist and can be kept because
  they have a topology/storage class pair that is still valid: `csistoragecapacities_desired_current`
- number of such objects that currently exist and need to be deleted
  because they have an outdated topology/storage class pair: `csistoragecapacities_obsolete`
- work queue length for creating, updating or deleting objects: `csistoragecapacity` work queue

The CSI driver name will be used as label. When using distributed
provisioning, the node name will be used as additional label.

* **What are the SLIs (Service Level Indicators) an operator can use to
  determine the health of the service?**

Pod status of the CSI driver deployment, existence of
`CSIStorageCapacity` objects and metrics data for `GetCapacity` calls
which are provided by the CSI sidecar as the
`csi_sidecar_operations_seconds` histogram with labels
`driver_name=<csi driver name>` and `method_name=GetCapacity`. This
way, both duration and total count are available.

Usually the `grpc_status_code` label will have `OK` as labels. Failed
calls will be recorded with their non-OK status code as value.

* **What are the reasonable SLOs (Service Level Objectives) for the above SLIs?**

The goal is to achieve the same provisioning rates with the feature
enabled as those that currently can be achieved without it.

The SLOs depend on the CSI driver and how they are deployed. Therefore SLOs
cannot be specified in more detail here. Cloud providers will have to determine
what reasonable values are and document those.

* **Are there any missing metrics that would be useful to have to improve
  observability if this feature?**

No.

### Dependencies

* **Does this feature depend on any specific services running in the cluster?**

For core Kubernetes just the ones that will also run without it enabled (apiserver,
kube-scheduler). Additional services are the CSI drivers.

 * CSI driver
   * Usage description:
     * Impact of its outage on the feature: pods that use the CSI driver will not
       be able to start
     * Impact of its degraded performance or high-error rates on the
       feature: When storage capacity information is not updated or
       not updated often enough, then pods are either not getting
       scheduled in cases where they could be scheduled (free capacity
       not reported) or they get tentatively scheduled onto nodes
       which do not have enough capacity (exhausted capacity not
       reported). To recover from the first scenario, the driver eventually
       needs to report capacity. To recover from the second scenario,
       volume creation attempts will fail with "resource exhausted" and
       other nodes have to be tried.

### Scalability

* **Will enabling / using this feature result in any new API calls?**

Yes.

Enabling it in apiserver and CSI drivers will cause
`CSIStorageCapacity` objects to be created or updated. The
number of those objects is proportional to the number of storage
classes and number of distinct storage topology segments. For
centralized provisioning, the number of segments is probably low. For
distributed provisioning, the each node where the driver runs
represents one segment, so the total number is total number of objects
is equal to the product of "number of nodes" and "number of storage
classes".

The rate at which objects depends on how often topology and storage
usage changes. It can estimated as:
* creating objects for each new node and deleting them when removing a
  node when using distributed provisioning
* the same for adding or removing storage classes (both modes)
* updates when volumes are created/resized/deleted (thus bounded by
  some other API calls)
* updates when capacity in the underlying storage system is changed
  (usually by an administrator)

Enabling it in kube-scheduler will cause it to cache all
`CSIStorageCapacity` objects via an informer.

* **Will enabling / using this feature result in introducing new API types?**

Yes, `CSIStorageCapacity`.

* **Will enabling / using this feature result in any new calls to cloud
  provider?**

A CSI driver might have to query the storage backend more often to be
kept informed about available storage capacity. This should only be
necessary for drivers using central provisioning and is mitigated
through rate limiting.

Distributed provisioning is expected to be used for local storage in
which case there is no cloud provider.

* **Will enabling / using this feature result in increasing size or count
  of the existing API objects?**

One new boolean field gets added to `CSIDriver`.

* **Will enabling / using this feature result in increasing time taken by any
  operations covered by [existing SLIs/SLOs][]?**

There is a SLI for [scheduling of pods without
volumes](https://github.com/kubernetes/community/blob/master/sig-scalability/slos/pod_startup_latency.md)
with a corresponding SLO. Those are not expected to be affected.

A SLI for scheduling of pods with volumes is work in progress. The SLO
for it will depend on the specific CSI driver.

* **Will enabling / using this feature result in non-negligible increase of
  resource usage (CPU, RAM, disk, IO, ...) in any components?**

Potentially in apiserver and kube-scheduler, but only if the feature
is actually used. Enabling it should not change anything.

### Troubleshooting

* **How does this feature react if the API server and/or etcd is unavailable?**

Pod scheduling stops (just as it does without the feature). Creation
and updating of `CSIStorageCapacity` objects is paused and will resume
when the API server becomes available again, with errors being logged
with exponential backoff in the meantime.

* **What are other known failure modes?**

The API server might get overloaded by CSIStorageCapacity updates.

* **What steps should be taken if SLOs are not being met to determine the problem?**

If enabling the feature in a CSI driver deployment should overload the
apiserver such that SLOs for the cluster are affected, then dashboards
for the apiserver should show an unusual number of operations related
to `CSIStorageCapacity` objects.

## Implementation History

- Kubernetes 1.19: alpha
- Kubernetes 1.21: beta
- Kubernetes 1.23: `CSIDriver.Spec.StorageCapacity` became mutable.
- Kubernetes 1.24: GA
