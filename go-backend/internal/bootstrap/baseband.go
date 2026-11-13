package bootstrap

import (
	"fmt"
	"os"
)

func basebandMode(fixture bool, online int) error {
	if os.Getenv("BASEBAND_ENABLED") != "1" {
		return nil
	}
	if fixture || online != 0 {
		return fmt.Errorf("BASEBAND_MODE_CONFLICT")
	}
	for _, key := range []string{"FEATURE_IMPACT_ENABLED", "CI_BUSINESS_QUALITY_ENABLED", "LOCALAGENT_ADAPTIVE_ENABLED", "LOCALAGENT_CI_SYNTHETIC_ENABLED", "LOCALAGENT_LOOP_ENABLED", "LOCALAGENT_AGENT_QUALITY_ENABLED", "LOCALAGENT_STAGE13_ENABLED"} {
		if os.Getenv(key) == "1" {
			return fmt.Errorf("BASEBAND_MODE_CONFLICT")
		}
	}
	return nil
}
