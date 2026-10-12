/********************************** (C) COPYRIGHT *******************************
 * File Name          : ws2812.c
 * Description        : WS2812B (NeoPixel) driver for CH32V003.
 *
 *  PD3 (TIM2_CH2) --> 7 x WS2812B in series.
 *
 *  Timing (48 MHz TIM2 clock, prescaler = 0):
 *      bit cell  = 1.25 us = 60 ticks  -> ARR = 59
 *      '0' high  = 0.40 us = 19 ticks  -> CCR = 19
 *      '1' high  = 0.80 us = 38 ticks  -> CCR = 38
 *      low time  = bit cell - high time (generated automatically)
 *
 *  The 3 low time constants above are within the WS2812B specification
 *  (T0H 0.4us +/-150ns, T1H 0.8us +/-150ns, period 1.25us +/-600ns).
 *******************************************************************************/
#include "ws2812.h"
#include "debug.h"

/* ------------------------------------------------------------------------- */
/* Timing constants                                                          */
/* ------------------------------------------------------------------------- */
#define WS2812_TIM_CLK_HZ           48000000UL

#if (WS2812_TIM_CLK_HZ != 48000000UL)
#error "WS2812 timing constants below are tuned for a 48 MHz timer clock"
#endif

/* 48 MHz / 800 kHz = 60 timer ticks per bit */
#define WS2812_BIT_TICKS            60U
/* 48 MHz * 0.40 us = 19.2 -> 19 ticks */
#define WS2812_T0H_TICKS            19U
/* 48 MHz * 0.80 us = 38.4 -> 38 ticks */
#define WS2812_T1H_TICKS            38U

#define WS2812_ARR_VALUE            (WS2812_BIT_TICKS - 1U)   /* 59 */

#define WS2812_BITS_PER_LED         24U
#define WS2812_BITS_TOTAL           (WS2812_LED_NUM * WS2812_BITS_PER_LED)

/* Reset / latch time that has to be kept low between two frames. */
#define WS2812_RESET_US             60U

/* ------------------------------------------------------------------------- */
/* Frame buffers                                                             */
/* ------------------------------------------------------------------------- */
/* GRB values of all LEDs, in transmission order. */
static uint8_t  ws2812_frame[WS2812_LED_NUM * 3];

/* PWM compare values, one entry per bit.  The extra trailing entry keeps the
 * line low (CCR = 0) once the frame has been shifted out, which also forms
 * the reset pulse of the next frame. */
static uint16_t ws2812_dma_buf[WS2812_BITS_TOTAL + 1U];

#define WS2812_DMA_SIZE             (WS2812_BITS_TOTAL + 1U)

/* ------------------------------------------------------------------------- */
/* Public API                                                                */
/* ------------------------------------------------------------------------- */

/*********************************************************************
 * @fn      WS2812_Init
 *
 * @brief   Configure PD3 as TIM2_CH2 PWM output and DMA1_Channel2 as the
 *          source of the PWM compare values.  The timer is left running
 *          with CCR = 0, so the data line idles low.
 *
 * @return  none
 */
void WS2812_Init(void)
{
    GPIO_InitTypeDef        GPIO_InitStructure = {0};
    TIM_TimeBaseInitTypeDef TIM_TimeBaseInitStructure = {0};
    TIM_OCInitTypeDef       TIM_OCInitStructure = {0};
    DMA_InitTypeDef         DMA_InitStructure = {0};

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOD, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM2, ENABLE);
    RCC_AHBPeriphClockCmd(RCC_AHBPeriph_DMA1, ENABLE);

    /* PD3 as alternate function push-pull (TIM2_CH2) */
    GPIO_InitStructure.GPIO_Pin = WS2812_GPIO_PIN;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_30MHz;
    GPIO_Init(WS2812_GPIO_PORT, &GPIO_InitStructure);

    /* TIM2 time base: 48 MHz / (0 + 1) = 48 MHz, 60 ticks per period */
    TIM_TimeBaseInitStructure.TIM_Period = WS2812_ARR_VALUE;
    TIM_TimeBaseInitStructure.TIM_Prescaler = 0;
    TIM_TimeBaseInitStructure.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseInitStructure.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseInit(TIM2, &TIM_TimeBaseInitStructure);

    /* CH2: PWM mode 1, active high while CNT < CH2CVR */
    TIM_OCInitStructure.TIM_OCMode = TIM_OCMode_PWM1;
    TIM_OCInitStructure.TIM_OutputState = TIM_OutputState_Enable;
    TIM_OCInitStructure.TIM_Pulse = 0;
    TIM_OCInitStructure.TIM_OCPolarity = TIM_OCPolarity_High;
    TIM_OC2Init(TIM2, &TIM_OCInitStructure);

    /* The compare value is written by DMA, it must take effect immediately
     * and must not be shadowed until the next update event. */
    TIM_OC2PreloadConfig(TIM2, TIM_OCPreload_Disable);
    TIM_ARRPreloadConfig(TIM2, ENABLE);

    /* DMA1_Channel2 services the TIM2 update request on CH32V003, and every
     * update event transfers one new CH2CVR value. */
    DMA_DeInit(DMA1_Channel2);
    DMA_InitStructure.DMA_PeripheralBaseAddr = (uint32_t)&TIM2->CH2CVR;
    DMA_InitStructure.DMA_MemoryBaseAddr = (uint32_t)ws2812_dma_buf;
    DMA_InitStructure.DMA_DIR = DMA_DIR_PeripheralDST;
    DMA_InitStructure.DMA_BufferSize = WS2812_DMA_SIZE;
    DMA_InitStructure.DMA_PeripheralInc = DMA_PeripheralInc_Disable;
    DMA_InitStructure.DMA_MemoryInc = DMA_MemoryInc_Enable;
    DMA_InitStructure.DMA_PeripheralDataSize = DMA_PeripheralDataSize_HalfWord;
    DMA_InitStructure.DMA_MemoryDataSize = DMA_MemoryDataSize_HalfWord;
    DMA_InitStructure.DMA_Mode = DMA_Mode_Normal;
    DMA_InitStructure.DMA_Priority = DMA_Priority_VeryHigh;
    DMA_InitStructure.DMA_M2M = DMA_M2M_Disable;
    DMA_Init(DMA1_Channel2, &DMA_InitStructure);

    /* Timer free running: CCR = 0 -> output stays low. */
    TIM_Cmd(TIM2, ENABLE);

    WS2812_Clear();
}

/*********************************************************************
 * @fn      WS2812_SetPixel
 *
 * @brief   Store one colour in the frame buffer.
 *
 * @param   index - LED index, 0 is the LED closest to the MCU.
 *          r/g/b - colour components, 0..255.
 *
 * @return  none
 */
void WS2812_SetPixel(uint8_t index, uint8_t r, uint8_t g, uint8_t b)
{
    uint8_t *p;

    if(index >= WS2812_LED_NUM)
    {
        return;
    }

    p = &ws2812_frame[index * 3U];
#if (WS2812_ORDER_GRB)
    p[0] = g;
    p[1] = r;
    p[2] = b;
#else
    p[0] = r;
    p[1] = g;
    p[2] = b;
#endif
}

/*********************************************************************
 * @fn      WS2812_SetAll
 *
 * @brief   Store the same colour in every LED of the frame buffer.
 *
 * @param   r/g/b - colour components, 0..255.
 *
 * @return  none
 */
void WS2812_SetAll(uint8_t r, uint8_t g, uint8_t b)
{
    uint8_t i;

    for(i = 0; i < WS2812_LED_NUM; i++)
    {
        WS2812_SetPixel(i, r, g, b);
    }
}

/*********************************************************************
 * @fn      WS2812_Clear
 *
 * @brief   Set the whole frame buffer to black.
 *
 * @return  none
 */
void WS2812_Clear(void)
{
    uint8_t i;

    for(i = 0; i < sizeof(ws2812_frame); i++)
    {
        ws2812_frame[i] = 0;
    }
}

/*********************************************************************
 * @fn      WS2812_Refresh
 *
 * @brief   Convert the frame buffer into PWM compare values and shift the
 *          resulting bit stream out to the LEDs.
 *
 * @return  WS2812_OK on success, WS2812_ERR_TIMEOUT if the DMA transfer did
 *          not complete.
 */
uint8_t WS2812_Refresh(void)
{
    uint16_t *dst = ws2812_dma_buf;
    uint8_t   i;
    uint8_t   bit;
    uint8_t   value;
    uint32_t  guard;

    /* Guarantee the reset/low time required by the WS2812B before a new
     * frame starts.  While the DMA is idle CCR = 0, so the line is low. */
    Delay_Us(WS2812_RESET_US);

    for(i = 0; i < sizeof(ws2812_frame); i++)
    {
        value = ws2812_frame[i];
        for(bit = 0; bit < 8U; bit++)
        {
            *dst++ = (value & 0x80U) ? WS2812_T1H_TICKS : WS2812_T0H_TICKS;
            value <<= 1;
        }
    }

    /* Trailing entry: keeps the line low until the next frame. */
    *dst = 0;

    /* Stop the whole chain and drop every pending flag before re-arming it.
     * The timer update flag is set by TIM_TimeBaseInit() (and by the last
     * update event of the previous frame); if the DMA request were enabled
     * while that flag is pending, one transfer would start immediately and
     * the whole frame would be shifted by one bit.  With CEN = 0 no new
     * update event can arrive while the channel is being armed. */
    DMA_Cmd(DMA1_Channel2, DISABLE);
    TIM_DMACmd(TIM2, TIM_DMA_Update, DISABLE);
    TIM_Cmd(TIM2, DISABLE);

    TIM_ClearFlag(TIM2, TIM_FLAG_Update);
    DMA_ClearFlag(DMA1_FLAG_TC2 | DMA1_FLAG_GL2 | DMA1_FLAG_HT2 | DMA1_FLAG_TE2);

    /* Arm the channel, reset the counter, then release the timer.  The first
     * transfer now happens exactly one bit period after CEN is set. */
    TIM_SetCounter(TIM2, 0);
    DMA_SetCurrDataCounter(DMA1_Channel2, WS2812_DMA_SIZE);
    DMA_Cmd(DMA1_Channel2, ENABLE);
    TIM_DMACmd(TIM2, TIM_DMA_Update, ENABLE);
    TIM_Cmd(TIM2, ENABLE);

    /* 169 bit cells -> ~211 us for seven LEDs.  The bounded wait turns a
     * wrong/unserviced DMA request into a reported error instead of a hang:
     * the loop runs at most ~0.2 s. */
    guard = 200000U;
    while(DMA_GetFlagStatus(DMA1_FLAG_TC2) == RESET)
    {
        if(--guard == 0U)
        {
            TIM_Cmd(TIM2, DISABLE);
            TIM_DMACmd(TIM2, TIM_DMA_Update, DISABLE);
            DMA_Cmd(DMA1_Channel2, DISABLE);
            DMA_ClearFlag(DMA1_FLAG_TC2 | DMA1_FLAG_GL2 | DMA1_FLAG_HT2 | DMA1_FLAG_TE2);
            return WS2812_ERR_TIMEOUT;
        }
    }

    /* The trailing buffer entry has already forced CCR = 0, so the timer can
     * be stopped right away and the data line stays low. */
    TIM_Cmd(TIM2, DISABLE);
    TIM_DMACmd(TIM2, TIM_DMA_Update, DISABLE);
    DMA_Cmd(DMA1_Channel2, DISABLE);
    DMA_ClearFlag(DMA1_FLAG_TC2 | DMA1_FLAG_GL2 | DMA1_FLAG_HT2 | DMA1_FLAG_TE2);

    return WS2812_OK;
}
