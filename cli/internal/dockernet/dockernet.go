// Package dockernet inspects and repairs the shared piso Docker networks.
// Compose will not mutate an already-created bridge network: when the live
// piso_vpc differs from the spec declared in compose/gateway.yaml, `docker
// compose up` tries to remove and recreate it — and fails with "network has
// active endpoints" while per-project worker containers (a different compose
// project) are attached. `piso up` / `setup --rebuild` must therefore
// reconcile the network first: detach the foreign worker containers and let
// compose recreate it, then re-attach them.
package dockernet

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Network and project names must match compose/gateway.yaml (`name:` pins).
// GatewayProjectName is the gateway compose project (gateway + monitor): its
// containers are reprovisioned by compose itself and must never be detached.
const (
	VPCName            = "piso_vpc"
	EgressName         = "piso_egress"
	GatewayProjectName = "piso"
)

// VPCConfig is the effective bridge configuration docker reports for a
// network. Only the fields compose pins in gateway.yaml are checked; anything
// else is left to Docker's defaults.
type VPCConfig struct {
	Driver     string
	Internal   bool
	EnableIPv6 bool
	Masquerade bool // com.docker.network.bridge.enable_ip_masquerade
}

// DesiredVPCConfig mirrors the `networks.vpc` block of compose/gateway.yaml.
// Keep in sync when that file changes: a mismatch is exactly what makes
// compose try to remove + recreate the network, so the CLI has to reconcile
// it before `compose up` runs.
func DesiredVPCConfig() VPCConfig {
	return VPCConfig{
		Driver:     "bridge",
		Internal:   false, // transparent egress: workers NAT off the bridge (no proxy)
		EnableIPv6: false,
		Masquerade: true, // unrouted hosts egress directly; ruled hosts are host-DNATed
	}
}

// InspectVPC reports whether piso_vpc exists and its effective bridge config.
// A missing network is not an error: compose up will create it from
// gateway.yaml.
func InspectVPC() (exists bool, cfg VPCConfig, err error) {
	out, runErr := dockerOutput("network", "inspect", VPCName, "--format",
		`{{.Driver}}|{{.Internal}}|{{.EnableIPv6}}|{{index .Options "com.docker.network.bridge.enable_ip_masquerade"}}`)
	if runErr != nil {
		if isMissingNetwork(out, runErr) {
			return false, VPCConfig{}, nil
		}
		return false, VPCConfig{}, fmt.Errorf("inspect %s: %s: %w", VPCName, out, runErr)
	}
	parts := strings.Split(out, "|")
	if len(parts) != 4 {
		return false, VPCConfig{}, fmt.Errorf("inspect %s: unexpected output %q", VPCName, out)
	}
	return true, VPCConfig{
		Driver:     strings.TrimSpace(parts[0]),
		Internal:   parseBool(parts[1]),
		EnableIPv6: parseBool(parts[2]),
		Masquerade: parseBool(parts[3]),
	}, nil
}

// PrepGateway reconciles piso_vpc before the gateway compose up. When the live
// network matches DesiredVPCConfig (or is missing — compose creates it) this
// is a no-op. On a mismatch compose would remove + recreate the network and
// fail while foreign containers hold endpoints, so:
//   - every attached container that is NOT owned by the gateway compose
//     project (gateway + monitor, which compose would reprovision) is
//     detached first; and
//   - the gateway project's own containers are force-removed, because an
//     earlier aborted run can leave compose's bookkeeping claiming they are
//     still attached (their endpoints are long gone) — compose then errors
//     "container ... is not connected to the network piso_vpc" during the
//     network teardown instead of recreating them.
//
// The detached workers must be re-attached by ReconnectWorkers after compose
// up: the rebuild gives them a new vpc IP.
func PrepGateway() error {
	exists, cfg, err := InspectVPC()
	if err != nil {
		return err
	}
	if !exists || cfg == DesiredVPCConfig() {
		return nil
	}
	fmt.Printf("piso: %s config changed (want %+v, live %+v) — restoring workers across the network rebuild\n",
		VPCName, DesiredVPCConfig(), cfg)
	members, err := vpcMembers()
	if err != nil {
		return err
	}
	for _, name := range members {
		if proj, _ := containerLabel(name, "com.docker.compose.project"); proj == GatewayProjectName {
			continue
		}
		if out, err := dockerOutput("network", "disconnect", "-f", VPCName, name); err != nil {
			return fmt.Errorf("disconnect %s from %s: %s: %w", name, VPCName, out, err)
		}
	}
	return removeProjectContainers(GatewayProjectName)
}

// ReconnectWorkers attaches every existing per-project worker container that
// is not currently on piso_vpc (compose just recreated the network, so any
// worker is detached until put back — including ones left detached by an
// earlier aborted run). Returns the newly attached names so the caller can
// refresh their slug↔IP registry entry. A worker that cannot attach is a
// warning, not a fatal error: the network migration already succeeded and a
// later `piso up` for that project re-attaches it.
func ReconnectWorkers() ([]string, error) {
	members, err := vpcMembers()
	if err != nil {
		return nil, err
	}
	attached := make(map[string]bool, len(members))
	for _, n := range members {
		attached[n] = true
	}
	workers, err := workerContainers()
	if err != nil {
		return nil, err
	}
	var reconnected []string
	for _, name := range workers {
		if attached[name] {
			continue
		}
		if err := Reconnect(name); err != nil {
			fmt.Fprintf(os.Stderr, "piso: warning: reattach %s: %v\n", name, err)
			continue
		}
		reconnected = append(reconnected, name)
	}
	return reconnected, nil
}

// Reconnect attaches a container that PrepGateway detached to the freshly
// created piso_vpc. Its vpc IP changes: reconnect before re-registering the
// slug↔IP entry with the gateway.
func Reconnect(name string) error {
	if out, err := dockerOutput("network", "connect", VPCName, name); err != nil {
		return fmt.Errorf("reconnect %s to %s: %s: %w", name, VPCName, out, err)
	}
	return nil
}

// workerContainers lists existing per-project worker containers (all
// piso-worker-* except the gateway project's monitor), running or not.
func workerContainers() ([]string, error) {
	out, err := dockerOutput("ps", "-a", "--filter", "name=piso-worker-", "--format", "{{.Names}}")
	if err != nil {
		return nil, fmt.Errorf("list worker containers: %s: %w", out, err)
	}
	workers := []string{}
	for _, name := range strings.Fields(out) {
		if name != "piso-worker-monitor" {
			workers = append(workers, name)
		}
	}
	return workers, nil
}

// removeProjectContainers force-removes one compose project's containers so
// compose recreates them from a clean slate. Missing containers are fine.
func removeProjectContainers(project string) error {
	out, err := dockerOutput("ps", "-aq", "--filter", "label=com.docker.compose.project="+project)
	if err != nil {
		return fmt.Errorf("list %s project containers: %s: %w", project, out, err)
	}
	ids := strings.Fields(out)
	if len(ids) == 0 {
		return nil
	}
	fmt.Printf("piso: removing stale %s-project containers for a clean network rebuild\n", project)
	if out, err := dockerOutput(append([]string{"rm", "-f"}, ids...)...); err != nil {
		return fmt.Errorf("remove %s-project containers: %s: %w", project, out, err)
	}
	return nil
}

// AssertVPC fails if piso_vpc is missing or does not match the declared spec.
// Call after gateway compose up so a stale or leaky network cannot silently
// survive.
func AssertVPC() error {
	exists, cfg, err := InspectVPC()
	if err != nil {
		return err
	}
	if !exists {
		return fmt.Errorf("%s is missing after compose up", VPCName)
	}
	if cfg != DesiredVPCConfig() {
		return fmt.Errorf("%s config mismatch after compose up: live %+v, want %+v", VPCName, cfg, DesiredVPCConfig())
	}
	return nil
}

// vpcMembers returns the names of every container currently attached to
// piso_vpc.
func vpcMembers() ([]string, error) {
	out, err := dockerOutput("network", "inspect", VPCName, "--format",
		"{{range $k, $v := .Containers}}{{$v.Name}} {{end}}")
	if err != nil {
		return nil, fmt.Errorf("list %s members: %s: %w", VPCName, out, err)
	}
	return strings.Fields(out), nil
}

// containerLabel reads one Config label from a container. Missing labels are
// reported as "" (the Go template prints "<no value>").
func containerLabel(name, key string) (string, error) {
	out, err := dockerOutput("inspect", "-f", `{{index .Config.Labels "`+key+`"}}`, name)
	if err != nil {
		return "", fmt.Errorf("inspect label %s of %s: %s: %w", key, name, out, err)
	}
	if out == "" || out == "<no value>" {
		return "", nil
	}
	return out, nil
}

// RemoveRuntime force-removes piso-gateway / piso-worker-* containers and
// the vpc/egress networks. Safe during testing; nothing is migrated.
func RemoveRuntime() error {
	idsOut, err := dockerOutput("ps", "-aq", "--filter", "name=piso-")
	if err != nil {
		return fmt.Errorf("list piso containers: %s: %w", idsOut, err)
	}
	ids := strings.Fields(idsOut)
	if len(ids) > 0 {
		args := append([]string{"rm", "-f"}, ids...)
		if out, rmErr := dockerOutput(args...); rmErr != nil {
			return fmt.Errorf("remove piso containers: %s: %w", out, rmErr)
		}
	}
	for _, name := range []string{VPCName, EgressName} {
		out, rmErr := dockerOutput("network", "rm", name)
		if rmErr != nil && !isMissingNetwork(out, rmErr) {
			return fmt.Errorf("remove network %s: %s: %w", name, out, rmErr)
		}
	}
	return nil
}

// parseBool interprets a docker inspect boolean field ({{.Internal}},
// {{.EnableIPv6}}, masquerade option). Unknown values are false.
func parseBool(out string) bool {
	return strings.EqualFold(strings.TrimSpace(out), "true")
}

// isMissingNetwork reports a docker inspect/rm "No such network" failure.
func isMissingNetwork(out string, err error) bool {
	if err == nil {
		return false
	}
	blob := strings.ToLower(out + " " + err.Error())
	return strings.Contains(blob, "no such network")
}

// LookPath finds the docker CLI. `sudo make install` drops privileges with a
// thin PATH, so we also probe common Desktop / OrbStack locations.
func LookPath() (string, error) {
	if p, err := exec.LookPath("docker"); err == nil {
		return p, nil
	}
	var candidates []string
	if home, err := os.UserHomeDir(); err == nil {
		candidates = append(candidates,
			filepath.Join(home, ".orbstack", "bin", "docker"),
			filepath.Join(home, ".docker", "bin", "docker"),
		)
	}
	candidates = append(candidates,
		"/usr/local/bin/docker",
		"/opt/homebrew/bin/docker",
		"/usr/bin/docker",
	)
	for _, c := range candidates {
		info, err := os.Stat(c)
		if err != nil || info.IsDir() {
			continue
		}
		return c, nil
	}
	return "", fmt.Errorf("docker not found in PATH; install Docker Desktop or OrbStack")
}

// dockerOutput runs `docker args...` and returns trimmed combined output.
func dockerOutput(args ...string) (string, error) {
	bin, err := LookPath()
	if err != nil {
		return "", err
	}
	cmd := exec.Command(bin, args...)
	out, err := cmd.CombinedOutput()
	return strings.TrimSpace(string(out)), err
}

// ContainerIPs returns the IPv4/v6 addresses the container is attached to,
// parsed from `docker inspect`. Used to populate the worker registry (slug↔IP)
// so the gateway can tag request logs.
func ContainerIPs(name string) ([]string, error) {
	out, err := dockerOutput("inspect", "-f", "{{range $n, $conf := .NetworkSettings.Networks}}{{$n}} {{$conf.IPAddress}} {{end}}", name)
	if err != nil {
		return nil, err
	}
	return parseContainerIPs(string(out)), nil
}

// parseContainerIPs extracts the IP tokens from `docker inspect` output that is
// a space-separated sequence of "<netname> <ip>" pairs. A token is an IP if it
// contains only IP characters (digits, dots, colons, hex letters); the network
// name tokens (e.g. "bzzz", "vpc") are discarded.
func parseContainerIPs(out string) []string {
	var ips []string
	for _, line := range strings.Split(out, " ") {
		if isIPString(line) {
			ips = append(ips, line)
		}
	}
	return ips
}

// isIPString reports a token that looks like a literal IPv4/IPv6 address:
// digits, dots, colons, or hex letters. Hostnames with letters beyond a-f are
// rejected (so network names like "vpc" / "bzzz" are not treated as IPs).
func isIPString(s string) bool {
	if s == "" || len(s) > 45 {
		return false
	}
	digits := 0
	for _, r := range strings.ToLower(s) {
		digit := r >= '0' && r <= '9'
		hex := r >= 'a' && r <= 'f'
		sep := r == '.' || r == ':' || r == '_'
		if !digit && !hex && !sep {
			return false
		}
		if digit {
			digits++
		}
	}
	return digits > 0
}
