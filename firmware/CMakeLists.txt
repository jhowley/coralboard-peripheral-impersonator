# Coralboard bench — Pico RP2040 target firmware (milestone 1, I2C only).
#
# Builds against a Pico SDK supplied via PICO_SDK_PATH (2.1.1 verified) and
# vmilea/pico_i2c_slave supplied via PICO_I2C_SLAVE_PATH. The verified build
# used arm-none-eabi-gcc 13.2.1 and produced a UF2 that was flashed and
# bench-tested; see docs/hardware-validation.md. Flashing is a separate,
# deliberate operator step. Build with:
#
#   export PICO_SDK_PATH=/path/to/pico-sdk
#   export PICO_I2C_SLAVE_PATH=/path/to/pico_i2c_slave
#   cmake -S firmware -B firmware/build
#   cmake --build firmware/build
#
# Both environment variables are honoured (no -D required). Produces
# coralboard_bench.uf2.

cmake_minimum_required(VERSION 3.13)

if(NOT DEFINED PICO_SDK_PATH AND DEFINED ENV{PICO_SDK_PATH})
    set(PICO_SDK_PATH $ENV{PICO_SDK_PATH})
endif()
if(NOT PICO_SDK_PATH)
    message(FATAL_ERROR "Set PICO_SDK_PATH to a pico-sdk checkout (see SDK_NOTES.md)")
endif()

# Explicit stack budget: the default 0x800 left no room for main + parser +
# I2C ISR. Large buffers are also .bss (main.c); 0x2000 is measured with
# -fstack-usage in the verified production build. crt0.S consumes
# PICO_STACK_SIZE, so
# define it before the SDK subdirectories are added.
if(NOT DEFINED PICO_STACK_SIZE)
    set(PICO_STACK_SIZE 0x2000)
endif()
add_compile_definitions(PICO_STACK_SIZE=${PICO_STACK_SIZE})

include(${PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(coralboard_bench_pico C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

pico_sdk_init()

# pico_i2c_slave (https://github.com/vmilea/pico_i2c_slave) is not vendored here.
if(NOT DEFINED PICO_I2C_SLAVE_PATH AND DEFINED ENV{PICO_I2C_SLAVE_PATH})
    set(PICO_I2C_SLAVE_PATH $ENV{PICO_I2C_SLAVE_PATH})
endif()
if(NOT DEFINED PICO_I2C_SLAVE_PATH)
    message(FATAL_ERROR
        "Set PICO_I2C_SLAVE_PATH to a pico_i2c_slave checkout; the I2C target "
        "library is a build input, not a copy inside this repo")
endif()
if(EXISTS ${PICO_I2C_SLAVE_PATH}/i2c_slave/CMakeLists.txt)
    add_subdirectory(${PICO_I2C_SLAVE_PATH}/i2c_slave pico_i2c_slave)
else()
    add_subdirectory(${PICO_I2C_SLAVE_PATH} pico_i2c_slave)
endif()

# The vmilea checkout defines target `i2c_slave`; newer SDKs define
# `pico_i2c_slave`. Accept either name rather than guessing.
if(TARGET i2c_slave)
    set(CB_I2C_SLAVE_TARGET i2c_slave)
elseif(TARGET pico_i2c_slave)
    set(CB_I2C_SLAVE_TARGET pico_i2c_slave)
else()
    message(FATAL_ERROR "PICO_I2C_SLAVE_PATH did not define i2c_slave or pico_i2c_slave")
endif()

add_executable(coralboard_bench
    src/main.c
    src/cmd.c
    src/core.c
    src/i2c_target.c
)
target_include_directories(coralboard_bench PRIVATE src)
# -fstack-usage emits a .su file per translation unit so the built frame sizes
# are evidence, not an estimate.
target_compile_options(coralboard_bench PRIVATE -Wall -Wextra -Werror
                                               -fstack-usage)
target_link_libraries(coralboard_bench
    pico_stdlib
    hardware_i2c
    hardware_irq
    hardware_sync
    ${CB_I2C_SLAVE_TARGET}
)

# USB CDC is the only stdio channel: UART stays silent.
pico_enable_stdio_usb(coralboard_bench 1)
pico_enable_stdio_uart(coralboard_bench 0)
pico_add_extra_outputs(coralboard_bench)
