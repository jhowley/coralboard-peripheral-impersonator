# Coralboard TESTER — Pico RP2040 I2C master fixture.
#
# Uses the Pico SDK supplied via PICO_SDK_PATH (2.1.1 verified) and does not
# build or modify the sensor firmware. Produces coralboard_tester.uf2.

cmake_minimum_required(VERSION 3.13)

if(NOT DEFINED PICO_SDK_PATH AND DEFINED ENV{PICO_SDK_PATH})
    set(PICO_SDK_PATH $ENV{PICO_SDK_PATH})
endif()
if(NOT PICO_SDK_PATH)
    message(FATAL_ERROR "Set PICO_SDK_PATH to a pico-sdk checkout (see README.md)")
endif()

include(${PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(coralboard_tester C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

pico_sdk_init()

add_executable(coralboard_tester
    src/main.c
    src/tester_core.c
    src/i2c_master.c
)
target_include_directories(coralboard_tester PRIVATE src)
target_compile_options(coralboard_tester PRIVATE -Wall -Wextra -Werror)
target_link_libraries(coralboard_tester
    pico_stdlib
    hardware_i2c
)

# Default SDK CDC descriptor and default USB strings/serial. This is the same
# 2e8a:000a layout the sensor firmware presents, so coralbench.usb_link can
# carry raw lines. Identity is asserted by PING, not by the descriptor.
pico_enable_stdio_usb(coralboard_tester 1)
pico_enable_stdio_uart(coralboard_tester 0)
pico_add_extra_outputs(coralboard_tester)
