/********************************** (C) COPYRIGHT *******************************
 * File Name          : buzzer.c
 * Description        : Passive buzzer driver for CH32V003 (PC7 = TIM1_CH2).
 *
 *  TIM1 is clocked at 48 MHz.  The PWM period is programmed to the wanted
 *  audio frequency, the duty cycle to the wanted volume:
 *
 *      PSC/ARR  -> frequency = 48 MHz / ((PSC + 1) * (ARR + 1))
 *      CCR      -> duty      = CCR / (ARR + 1)
 *
 *  TIM1 is a completely separate timer from TIM2, which drives the WS2812B
 *  data line, so both can run at the same time without disturbing each other.
 *******************************************************************************/
#include "buzzer.h"

#define BUZZER_TIM_CLK_HZ           48000000UL

/* Default idle tone setup: 2 kHz, output low. */
#define BUZZER_IDLE_PERIOD_TICKS    (BUZZER_TIM_CLK_HZ / 2000UL)

#define BUZZER_ARR_MAX              65535UL

/* Remaining time of the beep started with Buzzer_Beep(), in ms. */
static volatile uint16_t s_remaining_ms = 0;

/* ------------------------------------------------------------------------- */
/* Public API                                                                */
/* ------------------------------------------------------------------------- */

void Buzzer_Init(void)
{
    GPIO_InitTypeDef        GPIO_InitStructure = {0};
    TIM_TimeBaseInitTypeDef TIM_TimeBaseInitStructure = {0};
    TIM_OCInitTypeDef       TIM_OCInitStructure = {0};

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_AFIO | RCC_APB2Periph_GPIOC | RCC_APB2Periph_TIM1, ENABLE);

    /* TIM1 full remap: TIM1_CH1 = PC4, CH2 = PC7, CH3 = PC5 ...
     * Only PC7 is switched to alternate function, the other pins of the
     * remapped set stay general purpose IO.
     * Only the TIM1_REMAP field of AFIO_PCFR1 is written on purpose: the
     * vendor helper GPIO_PinRemapConfig() would additionally set the SWJ_CFG
     * field (AFIO_PCFR1[26:24]) to a reserved value. */
    AFIO->PCFR1 = (AFIO->PCFR1 & ~AFIO_PCFR1_TIM1_REMAP) | AFIO_PCFR1_TIM1_REMAP;

    GPIO_InitStructure.GPIO_Pin = BUZZER_GPIO_PIN;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_30MHz;
    GPIO_Init(BUZZER_GPIO_PORT, &GPIO_InitStructure);

    TIM_TimeBaseInitStructure.TIM_Period = (uint16_t)(BUZZER_IDLE_PERIOD_TICKS - 1UL);
    TIM_TimeBaseInitStructure.TIM_Prescaler = 0;
    TIM_TimeBaseInitStructure.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseInitStructure.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseInitStructure.TIM_RepetitionCounter = 0;
    TIM_TimeBaseInit(TIM1, &TIM_TimeBaseInitStructure);

    /* CH2: PWM mode 1, active high while CNT < CH2CVR, duty 0 = silent */
    TIM_OCInitStructure.TIM_OCMode = TIM_OCMode_PWM1;
    TIM_OCInitStructure.TIM_OutputState = TIM_OutputState_Enable;
    TIM_OCInitStructure.TIM_OutputNState = TIM_OutputNState_Disable;
    TIM_OCInitStructure.TIM_Pulse = 0;
    TIM_OCInitStructure.TIM_OCPolarity = TIM_OCPolarity_High;
    TIM_OC2Init(TIM1, &TIM_OCInitStructure);

    TIM_OC2PreloadConfig(TIM1, TIM_OCPreload_Disable);
    TIM_ARRPreloadConfig(TIM1, ENABLE);

    /* Advanced timer: the main output has to be enabled explicitly. */
    TIM_CtrlPWMOutputs(TIM1, ENABLE);
    TIM_Cmd(TIM1, ENABLE);

    s_remaining_ms = 0;
}

void Buzzer_Start(uint16_t freq_hz, uint8_t volume)
{
    uint32_t period_ticks;
    uint16_t prescaler;
    uint16_t arr;
    uint16_t ccr;

    if(volume > 100U)
    {
        volume = 100U;
    }

    if(volume == 0U)
    {
        Buzzer_Stop();
        return;
    }

    if(freq_hz < BUZZER_FREQ_MIN)
    {
        freq_hz = BUZZER_FREQ_MIN;
    }
    if(freq_hz > BUZZER_FREQ_MAX)
    {
        freq_hz = BUZZER_FREQ_MAX;
    }

    /* Total number of 48 MHz ticks in one audio period. */
    period_ticks = BUZZER_TIM_CLK_HZ / (uint32_t)freq_hz;
    if(period_ticks < 4UL)
    {
        period_ticks = 4UL;
    }

    /* Smallest prescaler that keeps ARR inside 16 bits, which leaves the
     * best possible duty cycle resolution. */
    prescaler = (uint16_t)((period_ticks - 1UL) / (BUZZER_ARR_MAX + 1UL));
    arr = (uint16_t)(period_ticks / ((uint32_t)prescaler + 1UL) - 1UL);

    TIM1->PSC = prescaler;
    TIM1->ATRLR = arr;
    /* Apply PSC/ARR immediately instead of at the next update event. */
    TIM_GenerateEvent(TIM1, TIM_EventSource_Update);

    /* volume 0..100 -> duty 0..50 % (see buzzer.h) */
    ccr = (uint16_t)(((uint32_t)(arr + 1U) * (uint32_t)volume) / BUZZER_DUTY_FULL_SCALE);
    TIM_SetCompare2(TIM1, ccr);
}

void Buzzer_Stop(void)
{
    TIM_SetCompare2(TIM1, 0);
    s_remaining_ms = 0;
}

void Buzzer_Beep(uint16_t freq_hz, uint8_t volume, uint16_t duration_ms)
{
    if((duration_ms == 0U) || (volume == 0U))
    {
        Buzzer_Stop();
        return;
    }

    Buzzer_Start(freq_hz, volume);
    s_remaining_ms = duration_ms;
}

void Buzzer_Task(void)
{
    if(s_remaining_ms != 0U)
    {
        s_remaining_ms--;
        if(s_remaining_ms == 0U)
        {
            Buzzer_Stop();
        }
    }
}

uint8_t Buzzer_IsBusy(void)
{
    return (s_remaining_ms != 0U) ? 1U : 0U;
}
