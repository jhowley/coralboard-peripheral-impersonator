#ifndef CB_CORE_PORT_H
#define CB_CORE_PORT_H

#include <stdint.h>

#ifdef CB_CORE_HOST
/* Host harness: single-threaded, no interrupt masking. */
static inline uint32_t cb_core_lock(void) { return 0; }
static inline void cb_core_unlock(uint32_t save) { (void)save; }
#else
#include "hardware/sync.h"
static inline uint32_t cb_core_lock(void) { return save_and_disable_interrupts(); }
static inline void cb_core_unlock(uint32_t save) { restore_interrupts(save); }
#endif

#endif /* CB_CORE_PORT_H */
