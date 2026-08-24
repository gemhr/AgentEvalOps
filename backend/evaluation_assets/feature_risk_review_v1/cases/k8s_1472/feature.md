## Summary

There are two types of volumes that are getting created after making a scheduling
decision for a pod:
- [ephemeral inline
  volumes](https://kubernetes-csi.github.io/docs/ephemeral-local-volumes.html) -
  a pod has been permanently scheduled onto a node
- persistent volumes with [delayed
  binding](https://kubernetes.io/docs/concepts/storage/storage-classes/#volume-binding-mode)
  (`WaitForFirstConsumer`) - a node has been selected tentatively

In both cases the Kubernetes scheduler currently picks a node without
knowing whether the storage system has enough capacity left for
creating a volume of the requested size. In the first case, `kubelet`
will ask the CSI node service to stage the volume. Depending on the
CSI driver, this involves creating the volume. In the second case, the
`external-provisioner` will note that a `PVC` is now ready to be
provisioned and ask the CSI controller service to create the volume
such that is usable by the node (via
[`CreateVolumeRequest.accessibility_requirements`](https://kubernetes-csi.github.io/docs/topology.html)).

If these volume operations fail, pod creation may get stuck. The
operations will get retried and might eventually succeed, for example
because storage capacity gets freed up or extended. A pod with an
ephemeral volume will not get rescheduled to another node. A pod with
a volume that uses delayed binding should get scheduled multiple times,
but then might always land on the same node unless there are multiple
nodes with equal priority.

A new API for exposing storage capacity currently available via CSI
drivers and a scheduler enhancement that uses this information will
reduce the risk of that happening.

## Motivation

### Goals

* Define an API for exposing storage capacity information.

* Expose capacity information at the semantic
  level that Kubernetes currently understands, i.e. in a way that
  Kubernetes can compare capacity against the requested size of
  volumes. This has to work for local storage, network-attached
  storage and for drivers where the capacity depends on parameters in
  the storage class.

* Support gathering that data for CSI drivers.

* Increase the chance of choosing a node for which volume creation
  will succeed by tracking the currently available capacity available
  through a CSI driver and using that information during pod
  scheduling.


### Non-Goals

* Drivers other than CSI will not be supported.

* No attempts will be made to model how capacity will be affected by
  pending volume operations. This would depend on internal driver
  details that Kubernetes doesn’t have.

* Nodes are not yet prioritized based on how much storage they have available.
  This and a way to specify the policy for the prioritization might be
  added later on.

* Because of that and also for other reasons (capacity changed via
  operations outside of Kubernetes, like creating or deleting volumes,
  or expanding the storage), it is expected that pod scheduling may
  still end up with a node from time to time where volume creation
  then fails. Rolling back in this case is complicated and outside of
  the scope of this KEP. For example, a pod might use two persistent
  volumes, of which one was created and the other not, and then it
  wouldn’t be obvious whether the existing volume can or should be
  deleted.

* For persistent volumes that get created independently of a pod
  nothing changes: it’s still the responsibility of the CSI driver to
  decide how to create the volume and then communicate back through
  topology information where pods using that volume need to run.
  However, a CSI driver may use the capacity information exposed
  through the proposed API to make its choice.

* Nothing changes for the current CSI ephemeral inline volumes. A [new
  approach for ephemeral inline
  volumes](https://github.com/kubernetes/enhancements/issues/1698)
  will support capacity tracking, based on this proposal here.

### User Stories

#### Ephemeral PMEM volume for Redis or memcached

A [modified Redis server](https://github.com/pmem/redis) and the upstream
version of [memcached](https://memcached.org/blog/persistent-memory/)
can use [PMEM](https://pmem.io/) as DRAM replacement with
higher capacity and lower cost at almost the same performance. When they
start, all old data is discarded, so an inline ephemeral volume is a
suitable abstraction for declaring the need for a volume that is
backed by PMEM and provided by
[PMEM-CSI](https://github.com/intel/pmem-csi). But PMEM is a resource
that is local to a node and thus the scheduler has to be aware whether
enough of it is available on a node before assigning a pod to it.

#### Different LVM configurations

A user may want to choose between higher performance of local disks
and higher fault tolerance by selecting striping respectively
mirroring or raid in the storage class parameters of a driver for LVM,
like for example [TopoLVM](https://github.com/cybozu-go/topolvm).

The maximum size of the resulting volume then depends on the storage
class and its parameters.

#### Network attached storage

In contrast to local storage, network attached storage can be made
available on more than just one node. However, for technical reasons
(high-speed network for data transfer inside a single data center) or
regulatory reasons (data must only be stored and processed in a single
jurisdication) availability may still be limited to a subset of the
nodes in a cluster.

#### Custom schedulers

For situations not handled by the Kubernetes scheduler now and/or in
the future, a [scheduler
extender](https://github.com/kubernetes/design-proposals-archive/blob/master/scheduling/scheduler_extender.md)
can influence pod scheduling based on the information exposed via the
new API. The
[topolvm-scheduler](https://github.com/cybozu-go/topolvm/blob/master/docs/design.md#how-the-scheduler-extension-works)
currently does that with a driver-specific way of storing capacity
information.

Alternatively, the [scheduling
framework](https://kubernetes.io/docs/concepts/configuration/scheduling-framework/)
can be used to build and run a custom scheduler where the desired
policy is compiled into the scheduler binary.

## Proposal

### Caching remaining capacity via the API server

The Kubernetes scheduler cannot talk directly to the CSI drivers to
retrieve capacity information because CSI drivers typically only
expose a local Unix domain socket and are not necessarily running on
the same host as the scheduler(s).

The key approach in this proposal for solving this is to gather
capacity information, store it in the API server, and then use that
information in the scheduler. That information then flows
through different components:
1. Storage backend
2. CSI driver
3. Kubernetes-CSI sidecar
4. API server
5. Kubernetes scheduler

The first two are driver specific. The sidecar will be provided by
Kubernetes-CSI, but how it is used is determined when deploying the
CSI driver. Steps 3 to 5 are explained below.

### Gathering capacity information

A sidecar, external-provisioner in this proposal, will be extended to
handle the management of the new objects. This follows the normal
approach that integration into Kubernetes is managed as part of the
CSI driver deployment, ideally without having to modify the CSI driver
itself.

### Pod scheduling

The Kubernetes scheduler watches the capacity information and excludes
nodes with insufficient remaining capacity when it comes to making
scheduling decisions for a pod which uses persistent volumes with
delayed binding. If the driver does not indicate that it supports
capacity reporting, then the scheduler proceeds just as it does now,
so nothing changes for existing CSI driver deployments.

## Design Details

### API

The API is a new builtin type. This approach was chosen instead of a
CRD because kube-scheduler will depend on this API and ensuring that a
CRD gets installed and updated together with the core Kubernetes is
still an unsolved problem.

#### CSIStorageCapacity

```
// CSIStorageCapacity stores the result of one CSI GetCapacity call.
// For a given StorageClass, this describes the available capacity in a
// particular topology segment.  This can be used when considering where to
// instantiate new PersistentVolumes.
//
// For example this can express things like:
// - StorageClass "standard" has "1234 GiB" available in "topology.kubernetes.io/zone=us-east1"
// - StorageClass "localssd" has "10 GiB" available in "kubernetes.io/hostname=knode-abc123"
//
// The following three cases all imply that no capacity is available for
// a certain combination:
// - no object exists with suitable topology and storage class name
// - such an object exists, but the capacity is unset
// - such an object exists, but the capacity is zero
//
// The producer of these objects can decide which approach is more suitable.
//
// They are consumed by the kube-scheduler when a CSI driver opts into capacity-aware
// scheduling with CSIDriverSpec.StorageCapacity. The scheduler compares the
// MaximumVolumeSize against the requested size of pending volumes to filter
// out unsuitable nodes. If MaximumVolumeSize is unset, it falls back to
// a comparison against the less precise Capacity. If that is also unset,
// the scheduler assumes that capacity is insufficient and tries some other node.
type CSIStorageCapacity struct {
	metav1.TypeMeta
	// Standard object's metadata. The name has no particular meaning. It must be
	// be a DNS subdomain (dots allowed, 253 characters). To ensure that
	// there are no conflicts with other CSI drivers on the cluster, the recommendation
	// is to use csisc-<uuid>, a generated name, or a reverse-domain name which ends
	// with the unique CSI driver name.
	//
	// Objects are namespaced.
	//
	// More info: https://git.k8s.io/community/contributors/devel/sig-architecture/api-conventions.md#metadata
	// +optional
	metav1.ObjectMeta

	// NodeTopology defines which nodes have access to the storage
	// for which capacity was reported. If not set, the storage is
	// not accessible from any node in the cluster. If empty, the
	// storage is accessible from all nodes.  This field is
	// immutable.
	//
	// +optional
	NodeTopology *metav1.LabelSelector

	// The name of the StorageClass that the reported capacity applies to.
	// It must meet the same requirements as the name of a StorageClass
	// object (non-empty, DNS subdomain). If that object no longer exists,
	// the CSIStorageCapacity object is obsolete and should be removed by its
	// creator.
	// This field is immutable.
	StorageClassName string

	// Capacity is the value reported by the CSI driver in its GetCapacityResponse
	// for a GetCapacityRequest with topology and parameters that match the
	// previous fields.
	//
	// The semantic is currently (CSI spec 1.2) defined as:
	// The available capacity, in bytes, of the storage that can be used
	// to provision volumes. If not set, that information is currently
	// unavailable.
	//
	// +optional
	Capacity *resource.Quantity

	// MaximumVolumeSize is the value reported by the CSI driver in its GetCapacityResponse
	// for a GetCapacityRequest with topology and parameters that match the
	// previous fields.
	//
	// This is defined since CSI spec 1.4.0 as the largest size
	// that may be used in a
	// CreateVolumeRequest.capacity_range.required_bytes field to
	// create a volume with the same parameters as those in
	// GetCapacityRequest. The corresponding value in the Kubernetes
	// API is ResourceRequirements.Requests in a volume claim.
	// Not all CSI drivers provide this information.
	//
	// +optional
	MaximumVolumeSize *resource.Quantity
}
```

Compared to the alternatives with a single object per driver (see
[`CSIDriver.Status`](#csidriverstatus) below) and one object per
topology (see [`CSIStoragePool`](#csistoragepool)), this approach has
the advantage that the size of the `CSIStorageCapacity` objects does
not increase with the potentially unbounded number of some other
objects (like storage classes).

The downsides are:
- Some attributes (storage class name, topology) must be stored multiple times
  compared to a more complex object, so overall data size in etcd is higher.
- Higher number of objects which all need to be retrieved by a client
  which does not already know which `CSIStorageCapacity` object it is
  interested in.

##### Example: local storage

```
apiVersion: storage.k8s.io/v1alpha1
kind: CSIStorageCapacity
metadata:
  name: csisc-ab96d356-0d31-11ea-ade1-8b7e883d1af1
storageClassName: some-storage-class
nodeTopology:
  nodeSelectorTerms:
  - matchExpressions:
    - key: kubernetes.io/hostname
      operator: In
      values:
      - node-1
capacity: 256G

apiVersion: storage.k8s.io/v1alpha1
kind: CSIStorageCapacity
metadata:
  name: csisc-c3723f32-0d32-11ea-a14f-fbaf155dff50
storageClassName: some-storage-class
nodeTopology:
  nodeSelectorTerms:
  - matchExpressions:
    - key: kubernetes.io/hostname
      operator: In
      values:
      - node-2
capacity: 512G
```

##### Example: affect of storage classes

```
apiVersion: storage.k8s.io/v1alpha1
kind: CSIStorageCapacity
metadata:
  name: csisc-9c17f6fc-6ada-488f-9d44-c5d63ecdf7a9
storageClassName: striped
nodeTopology:
  nodeSelectorTerms:
  - matchExpressions:
    - key: kubernetes.io/hostname
      operator: In
      values:
      - node-1
capacity: 256G

apiVersion: storage.k8s.io/v1alpha1
kind: CSIStorageCapacity
metadata:
  name: csisc-f0e03868-954d-11ea-9d78-9f197c0aea6f
storageClassName: mirrored
nodeTopology:
  nodeSelectorTerms:
  - matchExpressions:
    - key: kubernetes.io/hostname
      operator: In
      values:
      - node-1
capacity: 128G
```

##### Example: network attached storage

```
apiVersion: storage.k8s.io/v1alpha1
kind: CSIStorageCapacity
metadata:
  name: csisc-b0963bb5-37cf-415d-9fb1-667499172320
storageClassName: some-storage-class
nodeTopology:
  nodeSelectorTerms:
  - matchExpressions:
    - key: topology.kubernetes.io/region
      operator: In
      values:
      - us-east-1
availableCapacity: 128G

apiVersion: storage.k8s.io/v1alpha1
kind: CSIStorageCapacity
metadata:
  name: csisc-64103396-0d32-11ea-945c-e3ede5f0f3ae
storageClassName: some-storage-class
nodeTopology:
  nodeSelectorTerms:
  - matchExpressions:
    - key: topology.kubernetes.io/region
      operator: In
      values:
      - us-west-1
capacity: 256G
```

#### CSIDriver.spec.storageCapacity

A new field `storageCapacity` of type `boolean` with default `false`
in
[CSIDriver.spec](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#csidriverspec-v1-storage-k8s-io)
indicates whether a driver deployment will create `CSIStorageCapacity`
objects with capacity information and wants the Kubernetes scheduler
to rely on that information when making scheduling decisions that
involve volumes that need to be created by the driver.

If not set or false, the scheduler makes such decisions without considering
whether the driver really can create the volumes (the current situation).

This field was initially immutable for the sake of consistency with
the other fields. Deployments of a CSI driver had to delete and
recreate the object to switch from "feature disabled" to "feature
enabled". This turned out to be problematic:
- Seamless upgrades from a driver version without support of the feature
  to a version with support is harder (`kubectl apply` fails, operators
  need special cases to handle `CSIDriver`).
- Rolling out a driver without delaying pod scheduling and later
  enabling the check when the driver had published
  `CSIStorageCapacity` objects had to be done such that the driver was
  temporarily active without a `CSIDriver` object, which may have
  affected other aspects like skipping attach.

Starting with Kubernetes 1.23, the field can be modified. Clients
that assume that it is immutable work as before. The only consumer
is the Kubernetes scheduler. It supports mutability because it always
uses the current `CSIDriver` object from the informer cache.

### Updating capacity information with external-provisioner

Most (if not all) CSI drivers already get deployed on Kubernetes
together with the external-provisioner which then handles volume
provisioning via PVC.

Because the external-provisioner is part of the deployment of the CSI
driver, that deployment can configure the behavior of the
external-provisioner via command line parameters. There is no need to
introduce heuristics or other, more complex ways of changing the
behavior (like extending the `CSIDriver` API).

#### Available capacity vs. maximum volume size

The CSI spec up to and including the current version 1.2 just
specifies that ["the available
capacity"](https://github.com/container-storage-interface/spec/blob/314ac542302938640c59b6fb501c635f27015326/lib/go/csi/csi.pb.go#L2548-L2554)
is to be returned by the driver. It is [left open](https://github.com/container-storage-interface/spec/issues/432) whether that means
that a volume of that size can be created. This KEP uses the reported
capacity to rule out pools which clearly have insufficient storage
because the reported capacity is smaller than the size of a
volume. This will work better when CSI drivers implement `GetCapacity`
such that they consider constraints like fragmentation and report the
size that the largest volume can have at the moment.

#### Without central controller

[This mode of operation](https://github.com/kubernetes-csi/external-provisioner/blob/master/README.md#deployment-on-each-node), also called "distributed provisioning",
is expected to be used by CSI drivers that need
to track capacity per node and only support persistent volumes with
delayed binding that get provisioned by an external-provisioner
instance that runs on the node where the volume gets provisioned (see
the proposal for csi-driver-host-path in
https://github.com/kubernetes-csi/external-provisioner/pull/367).

The CSI driver has to implement the CSI controller service and its
`GetCapacity` call. Its deployment has to add the external-provisioner
to the daemon set and enable the per-node capacity tracking with
`--enable-capacity=local`.

The resulting `CSIStorageCapacity` objects then use a node selector for
one node.

#### With central controller

For central provisioning, external-provisioner gets deployed together
with a CSI controller service and capacity reporting gets enabled with
`--enable-capacity=central`. In this mode, CSI drivers must report
topology information in `NodeGetInfoResponse.accessible_topology` that
matches the storage pool(s) that it has access to, with granularity
that matches the most restrictive pool.

For example, if the driver runs in a node with region/rack topology
and has access to per-region storage as well as per-rack storage, then
the driver should report topology with region/rack as its keys. If it
only has access to per-region storage, then it should just use region
as key. If it uses region/rack, then the proposed approach below will
still work, but at the cost of creating redundant `CSIStorageCapacity`
objects.

Assuming that a CSI driver meets the above requirement and enables
this mode, external-provisioner then can identify pools as follows:
- iterate over all `CSINode` objects and search for
  `CSINodeDriver` information for the CSI driver,
- compute the union of the topology segments from these
  `CSINodeDriver` entries.

For each entry in that union, one `CSIStorageCapacity` object is
created for each storage class where the driver reports a non-zero
capacity. The node selector uses the topology key/value pairs as node
labels. That works because kubelet automatically labels nodes based on
the CSI drivers that run on that node.

#### Determining parameters

After determining the topology as described above,
external-provisioner needs to figure out with which volume parameters
it needs to call `GetCapacity`. It does that by iterating over all
storage classes that reference the driver.

If the current combination of topology segment and storage class
parameters do not make sense, then a driver must return "zero
capacity" or an error, in which case external-provisioner will skip
this combination. This covers the case where some storage class
parameter limits the topology segment of volumes using that class,
because information will then only be recorded for the segments that
are supported by the class.

#### CSIStorageCapacity lifecycle

external-provisioner needs permission to create, update and delete
`CSIStorageCapacity` objects. Before creating a new object, it must
check whether one already exists with the relevant attributes (driver
name + nodes + storage class) and then update that one
instead. Obsolete objects need to be removed.

To ensure that `CSIStorageCapacity` objects get removed when the driver
deployment gets removed before it has a chance to clean up, each
`CSIStorageCapacity` object should have an [owner
reference](https://godoc.org/k8s.io/apimachinery/pkg/apis/meta/v1#OwnerReference).

The owner should be the higher-level app object which defines the provisioner pods:
- the Deployment or StatefulSet for central provisioning
- the DaemonSet for distributed provisioning

This way, provisioning and pod scheduling can continue seamlessly when
there are multiple instances with leadership election or the driver
gets upgraded.

This ownership reference is optional. It gets enabled with
external-provisioner command line arguments when deploying the
driver. It has to be optional because drivers that aren't deployed
inside Kubernetes have nothing that can serve as owner. It's supported
for those cases where it can be used because cluster administrators do
not need to worry about manually deleting automatically created
objects after uninstalling a CSI driver.

Without it, administrators must manually delete
`CSIStorageCapacity` objects after uninstalling a CSI driver. To
simplify that and for efficient access to objects, external-provisioner
sets some labels:
- `csi.storage.k8s.io/drivername`: the CSI driver name
- `csi.storage.k8s.io/managed-by`: `external-provisioner` for central provisioning,
  `external-provisioner-<node name>` for distributed provisioning

Using those labels, `kubectl delete csistoragecapacities -l
csi.storage.k8s.io/drivername=my-csi.example.com` will delete just the
objects of that driver.

It is possible to create `CSIStorageCapacity` manually. This may become
useful at some point to provide information which cannot be retrieved through
a running CSI driver instance. external-provisioner must not delete those.
In external-provisioner 2.0.0, this was achieved by checking the owner
reference. Now that the owner reference is optional, it will get
achieved by ignoring all objects that don't have the
`csi.storage.k8s.io/managed-by` labels described above.

If such objects are created manually, then it is the responsibility of
the person creating them to ensure that the information does not
conflict with objects that external-provisioner has created or will
create, i.e. the topology and/or storage class must be
different. kube-scheduler will schedule pods using whatever object if
finds first without checking for consistency, because such consistency
checks would affect performance. As explained below,
external-provisioner cannot detect them either.

While external-provisioner runs, it potentially needs to update or
delete `CSIStorageCapacity` objects:
- when nodes change (for central provisioning)
- when storage classes change
- when volumes were created or deleted
- when volumes are resized or snapshots are created or deleted (for persistent volumes)
- periodically, to detect changes in the underlying backing store (all cases)

In each of these cases, it needs to verify that all currently existing
`CSIStorageCapacity` objects are still needed (i.e. match one of the
current combinations of topology and parameters) and delete the
obsolete ones. Missing objects need to be created. For existing ones,
`GetCapacity` has to be called and if the result is different than
before, the object needs to be updated.

Because sidecars are currently separated, external-provisioner is
unaware of resizing and snapshotting. The periodic polling will catch up
with changes caused by those operations.

For efficiency reasons, external-provisioner instances watch
`CSIStorageCapacity` objects in their namespace and filter already in
the apiserver by the `csi.storage.k8s.io/drivername` and
`csi.storage.k8s.io/managed-by` labels. This is the reason why
external-provisioner cannot detect when manually created objects
conflict with the ones created automatically because it will never
receive them.

When using distributed provisioning, the effect is that objects become
orphaned when the node that they were created for no longer has a
running CSI driver or the node itself was removed: they are neither
garbage collected because the DaemonSet still exists, nor do they get
deleted by external-provisioner because all remaining
external-provisioner instances ignore them.

To solve this, external-provisioner can be deployed centrally with the
purpose of cleaning up such orphaned objects. It does not need a
running CSI driver for this, just the CSI driver name as a fixed
parameter. It then watches `CSINode` objects and if the CSI driver has
not been running on that node for a certain amount of time (again set
via a parameter), it will remove all objects for that node. This
approach is simpler than trying to determine whether the CSI driver
should be running on a node and it also covers the case where a CSI
driver for some reason fails to run on the node. By removing the
`CSIStorageCapacity` objects for that node, kube-scheduler will stop
choosing it for pods which have unbound volumes.

### Using capacity information

The Kubernetes scheduler already has a component, the [volume
scheduling
library](https://github.com/kubernetes/kubernetes/tree/master/pkg/controller/volume/scheduling),
which implements [topology-aware
scheduling](https://github.com/kubernetes/design-proposals-archive/blob/master/storage/volume-topology-scheduling.md).

The
[CheckVolumeBinding](https://github.com/kubernetes/design-proposals-archive/blob/master/storage/volume-topology-scheduling.md#integrating-volume-binding-with-pod-scheduling)
function gets extended to not only check for a compatible topology of
a node (as it does now), but also to verify whether the node falls
into a topology segment that has enough capacity left. This check is
only necessary for PVCs that have not been bound yet.

The lookup sequence will be:
- find the `CSIDriver` object for the driver
- check whether it has `CSIDriver.spec.storageCapacity` enabled
- find all `CSIStorageCapacity` objects that have the right spec
  (driver, accessible by node, storage class) and sufficient capacity.

The specified volume size is compared against `Capacity` if
available. A topology segment which has no reported capacity or a
capacity that is too small is considered unusable at the moment and
ignored.

Each volume gets checked separately, independently of other volumes
that are needed by the current pod or by volumes that are about to be
created for other pods. Those scenarios remain problematic.

Trying to model how different volumes affect capacity would be
difficult. If the capacity represents "maximum volume size 10GiB", it may be possible
to create exactly one such volume or several, so rejecting the
node after one volume could be a false negative. With "available
capacity 10GiB" it may or may not be possible to create two volumes of
5GiB each, so accepting the node for two such volumes could be a false
positive.

More promising might be to add prioritization of nodes based on how
much capacity they have left, thus spreading out storage usage evenly.
This is a likely future extension of this KEP.

Either way, the problem of recovering more gracefully from running out
of storage after a bad scheduling decision will have to be addressed
eventually. Details for that are in https://github.com/kubernetes/enhancements/pull/1703.
