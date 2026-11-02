# Convenience entry points. Every target is offline except the two firmware
# builds, which use the SDK paths supplied in the environment.

.PHONY: test build-sensor build-tester check clean

test:
	sh scripts/run_offline_tests.sh

build-sensor:
	sh scripts/build_firmware.sh

build-tester:
	sh scripts/build_tester.sh

check:
	sh scripts/check_public_tree.sh

clean:
	rm -rf build firmware/build tester/build tester/build-host
