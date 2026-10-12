/********************************** (C) COPYRIGHT *******************************
 * File Name          : uart.c
 * Description        : USART1 driver for CH32V003 (PD5 = TX, PD6 = RX).
 *******************************************************************************/
#include "uart.h"

/* ------------------------------------------------------------------------- */
/* Receive ring buffer, written by the USART1 interrupt, read by the main    */
/* loop.  head is advanced by the ISR, tail by the consumer.                 */
/* ------------------------------------------------------------------------- */
static volatile uint8_t  s_rx_buffer[UART_RX_BUFFER_SIZE];
static volatile uint16_t s_rx_head = 0;
static volatile uint16_t s_rx_tail = 0;

/* ------------------------------------------------------------------------- */
/* Public API                                                                */
/* ------------------------------------------------------------------------- */

void UART_Init(uint32_t baudrate)
{
    GPIO_InitTypeDef  GPIO_InitStructure = {0};
    USART_InitTypeDef USART_InitStructure = {0};
    NVIC_InitTypeDef  NVIC_InitStructure = {0};

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOD | RCC_APB2Periph_USART1, ENABLE);

    /* USART1 default mapping: TX -> PD5, RX -> PD6 */
    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_5;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_30MHz;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF_PP;
    GPIO_Init(GPIOD, &GPIO_InitStructure);

    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_6;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_IN_FLOATING;
    GPIO_Init(GPIOD, &GPIO_InitStructure);

    USART_InitStructure.USART_BaudRate = baudrate;
    USART_InitStructure.USART_WordLength = USART_WordLength_8b;
    USART_InitStructure.USART_StopBits = USART_StopBits_1;
    USART_InitStructure.USART_Parity = USART_Parity_No;
    USART_InitStructure.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    USART_InitStructure.USART_Mode = USART_Mode_Tx | USART_Mode_Rx;
    USART_Init(USART1, &USART_InitStructure);

    /* Receive interrupt: one byte in, one byte out of the ring buffer.
     * NVIC_PriorityGroup_1 is selected in main(), which allows preemption
     * priorities 0..1 (see ch32v00x_misc.c). */
    NVIC_InitStructure.NVIC_IRQChannel = USART1_IRQn;
    NVIC_InitStructure.NVIC_IRQChannelPreemptionPriority = 1;
    NVIC_InitStructure.NVIC_IRQChannelSubPriority = 0;
    NVIC_InitStructure.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&NVIC_InitStructure);

    USART_ITConfig(USART1, USART_IT_RXNE, ENABLE);
    USART_Cmd(USART1, ENABLE);
}

void UART_SendByte(uint8_t data)
{
    while(USART_GetFlagStatus(USART1, USART_FLAG_TXE) == RESET)
    {
    }
    USART_SendData(USART1, data);
}

void UART_SendBuffer(const uint8_t *data, uint16_t len)
{
    uint16_t i;

    for(i = 0; i < len; i++)
    {
        UART_SendByte(data[i]);
    }

    /* Make sure the last byte has really left the shift register before the
     * caller (e.g. a protocol reply) returns. */
    while(USART_GetFlagStatus(USART1, USART_FLAG_TC) == RESET)
    {
    }
}

void UART_SendString(const char *str)
{
    while(*str != '\0')
    {
        UART_SendByte((uint8_t)*str++);
    }

    while(USART_GetFlagStatus(USART1, USART_FLAG_TC) == RESET)
    {
    }
}

uint16_t UART_Available(void)
{
    return (uint16_t)((s_rx_head + UART_RX_BUFFER_SIZE - s_rx_tail) % UART_RX_BUFFER_SIZE);
}

int UART_ReadByte(uint8_t *data)
{
    if(s_rx_tail == s_rx_head)
    {
        return 0;
    }

    *data = s_rx_buffer[s_rx_tail];
    s_rx_tail = (uint16_t)((s_rx_tail + 1U) % UART_RX_BUFFER_SIZE);
    return 1;
}

/* ------------------------------------------------------------------------- */
/* USART1 interrupt                                                          */
/* ------------------------------------------------------------------------- */

void USART1_IRQHandler(void) __attribute__((interrupt("WCH-Interrupt-fast")));

void USART1_IRQHandler(void)
{
    /* Reading STATR and then DATAR clears both RXNE and the overrun error. */
    uint16_t status = USART1->STATR;

    if((status & (USART_STATR_RXNE | USART_STATR_ORE)) != 0U)
    {
        uint8_t data = (uint8_t)USART1->DATAR;

        if((status & USART_STATR_RXNE) != 0U)
        {
            uint16_t next = (uint16_t)((s_rx_head + 1U) % UART_RX_BUFFER_SIZE);

            if(next != s_rx_tail)   /* drop the byte when the buffer is full */
            {
                s_rx_buffer[s_rx_head] = data;
                s_rx_head = next;
            }
        }
    }
}
