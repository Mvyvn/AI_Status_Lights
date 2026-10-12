/********************************** (C) COPYRIGHT *******************************
 * File Name          : protocol.h
 * Description        : UART command protocol for the AI Status Lights.
 *
 *  Every frame has the same shape:
 *
 *      55 AA | MODE | PAYLOAD... | CHECKSUM | BB
 *
 *  CHECKSUM = (sum of all bytes from the leading 0x55 up to and including the
 *             last payload byte) & 0xFF
 *
 *  MODE 0x00  VERIFY   payload: none                       (total 5 bytes)
 *                     -> the firmware answers with the fixed string
 *                        "AI Status Lights OK\r\n"
 *
 *  MODE 0x01  LEDS     payload: 21 bytes = R0 G0 B0 ... R6 G6 B6
 *                     (7 LEDs x 3 bytes, index 0 = LED closest to the MCU,
 *                      colour order on the wire is RGB, the driver converts
 *                      it to the WS2812B GRB order)
 *                     -> the new colours are shown immediately (total 26 bytes)
 *
 *  MODE 0x02  BUZZER   payload: 5 bytes = freq_lo freq_hi volume dur_lo dur_hi
 *                     freq     : 16 bit little endian, Hz (clamped 20..20000)
 *                     volume   : 8 bit, 0..100
 *                     duration : 16 bit little endian, ms
 *                     -> the buzzer sounds non blocking for the given time
 *                        (total 10 bytes)
 *
 *  Unknown modes, bad checksums and bad frame tails are discarded and the
 *  parser resynchronises on the next 0x55.
 *******************************************************************************/
#ifndef __PROTOCOL_H
#define __PROTOCOL_H

#include "ch32v00x.h"
#include "ws2812.h"

/* Frame delimiters. */
#define PROTO_HEAD1                 0x55U
#define PROTO_HEAD2                 0xAAU
#define PROTO_TAIL                  0xBBU

/* Frame modes. */
#define PROTO_MODE_VERIFY           0x00U
#define PROTO_MODE_LEDS             0x01U
#define PROTO_MODE_BUZZER           0x02U

/* Payload lengths. */
#define PROTO_LED_PAYLOAD_LEN       (WS2812_LED_NUM * 3U)   /* 21 */
#define PROTO_BUZZER_PAYLOAD_LEN    5U

/* Longest payload the parser has to buffer. */
#define PROTO_MAX_PAYLOAD_LEN       21U

/* If no byte of a started frame arrives for this long, the parser throws the
 * half received frame away and waits for a new header.  At 115200 baud a
 * complete frame takes about 2.3 ms, so a sender that stalls for 20 ms in the
 * middle of a frame is treated as broken.  Protocol_Task() has to be called
 * more often than this. */
#define PROTO_FRAME_TIMEOUT_MS      20U

/* Fixed answer of the verify command. */
#define PROTO_VERIFY_REPLY          "AI Status Lights OK\r\n"

/*********************************************************************
 * @fn      Protocol_Init
 *
 * @brief   Reset the frame parser state.
 *
 * @return  none
 */
void Protocol_Init(void);

/*********************************************************************
 * @fn      Protocol_Task
 *
 * @brief   Consume everything the UART has received and execute the complete
 *          frames found in it.  Call this regularly from the main loop.
 *
 * @return  none
 */
void Protocol_Task(void);

#endif /* __PROTOCOL_H */
