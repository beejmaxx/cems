CXX = clang++
CXXFLAGS = -std=c++20 -O3 -Wall -Wextra -Werror
CPPFLAGS = -I. -Isrc/native
BUILD = build/workspace-v1
export PYTHONPATH := $(abspath src):$(abspath tools)$(if $(PYTHONPATH),:$(PYTHONPATH))
TEST_PYTHONPATH := $(abspath src):$(abspath tools):$(abspath tools/experiments):$(abspath tests)$(if $(PYTHONPATH),:$(PYTHONPATH))
PYTHON ?= python3
PREFIX ?= $(HOME)/.local
ifneq ($(strip $(WORKSPACE)),)
export RECOVERY_WORKSPACE := $(abspath $(WORKSPACE))
endif

test sanitize test-large sanitize-large test-luks1 sanitize-luks1 test-gpu-handoff: export PYTHONPATH := $(TEST_PYTHONPATH)

.PHONY: all build help test sanitize test-large sanitize-large
all: $(BUILD)/prep $(BUILD)/prep-checked $(BUILD)/prep-compact $(BUILD)/prep-workspace $(BUILD)/synthetic-checker $(BUILD)/set-geometry
build: all

.PHONY: install test-cli
install: $(BUILD)/prep-workspace
	$(PYTHON) -B tools/install_cli.py --prefix "$(PREFIX)"

test-cli: $(BUILD)/prep-workspace
	PYTHONPATH="$(TEST_PYTHONPATH)" PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -B -m unittest -v test_plan_inspect test_recipe test_recipe_desk test_recipe_views test_workspace

help:
	@echo "make install       Install cems into $(PREFIX)/bin"
	@echo "make test-cli      Test installed CLI and rank sampling"
	@echo "make build test test-large; optional: test-luks1 test-gpu-handoff"

# Generic language set operations used by coverage and campaign migration.
$(BUILD)/set-geometry: src/native/set_geometry.cpp src/native/core_checked.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@

$(BUILD)/set-geometry-sanitized: src/native/set_geometry.cpp src/native/core_checked.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer $< -o $@

# Independent TEST consumer. It does not replace any pinned checker or generator.
$(BUILD)/recipe-audit: tests/native/recipe_audit.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@

$(BUILD)/recipe-audit-sanitized: tests/native/recipe_audit.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer $< -o $@

$(BUILD):
	mkdir -p $@

$(BUILD)/prep: src/native/core.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@

# The original binary is pinned by existing campaigns. Never replace it with
# changed semantics in-place. New campaigns explicitly select prep-checked.
$(BUILD)/prep-checked: src/native/core_checked.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@

$(BUILD)/prep-compact: src/native/prep_compact.cpp src/native/core_checked.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@

$(BUILD)/prep-workspace: src/native/prep_workspace.cpp src/native/core_checked.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@

$(BUILD)/synthetic-checker: tests/native/synthetic_checker.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@

$(BUILD)/synthetic-checker-sanitized: tests/native/synthetic_checker.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer $< -o $@

$(BUILD)/prep-sanitized: src/native/core.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer $< -o $@

$(BUILD)/prep-checked-sanitized: src/native/core_checked.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer $< -o $@

$(BUILD)/prep-compact-sanitized: src/native/prep_compact.cpp src/native/core_checked.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer $< -o $@

$(BUILD)/prep-workspace-sanitized: src/native/prep_workspace.cpp src/native/core_checked.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer $< -o $@

test: all
	PREP_BINARY=$(BUILD)/prep-workspace PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest -v test_core test_runner test_checkpoint test_validation test_stress_oracle test_history_oracle test_model_workflow test_workspace test_plan_inspect test_recipe test_recipe_desk test_recipe_views test_set_geometry test_history_campaign

sanitize: all $(BUILD)/prep-workspace-sanitized $(BUILD)/synthetic-checker-sanitized $(BUILD)/set-geometry-sanitized
	PREP_BINARY=$(BUILD)/prep-workspace-sanitized CHECKER_BINARY=$(BUILD)/synthetic-checker-sanitized LEGACY_GEOMETRY=$(BUILD)/set-geometry-sanitized PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest -v test_core test_runner test_checkpoint test_validation test_stress_oracle test_history_oracle test_model_workflow test_workspace test_set_geometry test_history_campaign

# Reduced exhaustive/fault tests for the independent large-campaign oracle.
# The actual 100M-check experiment is an explicit large_story.py invocation.
test-large: $(BUILD)/prep-workspace $(BUILD)/recipe-audit
	PREP_BINARY=$(BUILD)/prep-workspace RECIPE_AUDITOR=$(BUILD)/recipe-audit PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest -v test_large_story

sanitize-large: $(BUILD)/prep-workspace-sanitized $(BUILD)/recipe-audit-sanitized
	PREP_BINARY=$(BUILD)/prep-workspace-sanitized RECIPE_AUDITOR=$(BUILD)/recipe-audit-sanitized PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest -v test_large_story

# Optional real-cryptography reference adapter. Does not replace any frozen
# equality checker or existing pinned binary. Static libcrypto pins crypto code
# inside this executable; QEMU is independently hashed by the confirmer.
OPENSSL_CFLAGS = $(shell pkg-config --cflags libcrypto)
LIBCRYPTO_STATIC = $(shell pkg-config --variable=libdir libcrypto)/libcrypto.a
CRYPTO_SYSTEM_LIBS = $(filter-out -lcrypto,$(shell pkg-config --libs-only-l --libs-only-other --static libcrypto))
$(BUILD)/luks1-checker: src/native/luks1_checker.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(OPENSSL_CFLAGS) $< $(LIBCRYPTO_STATIC) $(CRYPTO_SYSTEM_LIBS) -pthread -o $@

$(BUILD)/luks1-checker-sanitized: src/native/luks1_checker.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer $(OPENSSL_CFLAGS) $< $(LIBCRYPTO_STATIC) $(CRYPTO_SYSTEM_LIBS) -pthread -o $@

.PHONY: test-luks1 sanitize-luks1
test-luks1: $(BUILD)/prep-workspace $(BUILD)/luks1-checker
	PREP_BINARY=$(BUILD)/prep-workspace PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -B -m unittest -v test_luks1_checker

sanitize-luks1: $(BUILD)/prep-workspace-sanitized $(BUILD)/luks1-checker-sanitized
	PREP_BINARY=$(BUILD)/prep-workspace-sanitized LUKS1_CHECKER=$(BUILD)/luks1-checker-sanitized PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -B -m unittest -v test_luks1_checker

# Separate optional GPU handoff; frozen engine/checker identities stay intact.
$(BUILD)/hex-spool: src/native/hex_spool.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@

$(BUILD)/hex-spool-sanitized: src/native/hex_spool.cpp | $(BUILD)
	$(CXX) $(CPPFLAGS) -std=c++20 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer $< -o $@

.PHONY: gpu-handoff test-gpu-handoff
gpu-handoff: all $(BUILD)/luks1-checker $(BUILD)/hex-spool

test-gpu-handoff: gpu-handoff
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -B -m unittest -v test_gpu_handoff

.PHONY: whitepaper
TYPST ?= typst
whitepaper: WHITEPAPER.pdf

WHITEPAPER.pdf: WHITEPAPER.typ WHITEPAPER.bib whitepaper/pipeline.svg
	$(TYPST) compile WHITEPAPER.typ $@
