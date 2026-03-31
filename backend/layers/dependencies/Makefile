# SAM calls this when BuildMethod: makefile is set on the layer.
# Downloads x86_64 Linux wheels so the layer works on Lambda (x86_64)
# even when building from an ARM64 machine (Raspberry Pi).

build-DependenciesLayer:
	pip3 install \
		--platform manylinux2014_x86_64 \
		--target "$(ARTIFACTS_DIR)/python" \
		--implementation cp \
		--python-version 3.12 \
		--only-binary=:all: \
		--upgrade \
		-r requirements.txt
