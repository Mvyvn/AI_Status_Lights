/********************************** (C) COPYRIGHT *******************************
 * File Name          : uart.h
 * Description        : USART1 driver for CH32V003.
 *
 *  PD5 -> USART1_TX (alternate function push-pull)
 *  PD6 -> USART1_RX (floating input)
 *  115200 8-N-1 by default, RX is interrupt driven into a ring buffer so
 *  that incoming protocol frames are never lost while the main loop is busy.
 *******************************************************************************/
#ifndef __UART_H
#define __UART_H

#include "ch32v00x.h"

/* Receive ring buffer, must be a power of two friendly value for the
 * modulo arithmetic used below. */
#define UART_RX_BUFFER_SIZE         128U

/* Default line settings: 115200 8-N-1. */
#define UART_DEFAULT_BAUD           115200U

/*********************************************************************
 * @fn      UART_Init
 *
 * @brief   Configure PD5/PD6 as USART1 TX/RX at the given baud rate and
 *          enable the receive interrupt.
 *
 * @param   baudrate - e.g. 115200.
 *
 * @return  none
 */
void     UART_Init(uint32_t baudrate);

/*********************************************************************
 * @fn      UART_SendByte
 *
 * @brief   Send one byte, blocking until it has been loaded.
 *
 * @param   data - byte to send.
 *
 * @return  none
 */
void     UART_SendByte(uint8_t data);

/*********************************************************************
 * @fn      UART_SendBuffer
 *
 * @brief   Send a block of bytes, blocking until the last one is on the wire.
 *
 * @param   data - pointer to the bytes.
 *          len  - number of bytes.
 *
 * @return  none
 */
void     UART_SendBuffer(const uint8_t *data, uint16_t len);

/*********************************************************************
 * @fn      UART_SendString
 *
 * @brief   Send a NUL terminated string.
 *
 * @param   str - string to send.
 *
 * @return  none
 */
void     UART_SendString(const char *str);

/*********************************************************************
 * @fn      UART_Available
 *
 * @brief   Number of bytes waiting in the receive ring buffer.
 *
 * @return  byte count
 */
uint16_t UART_Available(void);

/*********************************************************************
 * @fn      UART_ReadByte
 *
 * @brief   Fetch one byte from the receive ring buffer.
 *
 * @param   data - destination for the byte.
 *
 * @return  1 when a byte was returned, 0 when the buffer is empty
 */
int      UART_ReadByte(uint8_t *data);

#endif /* __UART_H */
