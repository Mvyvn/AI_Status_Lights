/********************************** (C) COPYRIGHT *******************************
 * File Name          : buzzer.h
 * Description        : Passive buzzer driver for CH32V003.
 *
 *  PC7 -> TIM1_CH2 (requires the TIM1 full remap, confirmed by WCH example
 *         EXAM/TIM/PWM_6_Step: with GPIO_FullRemap_TIM1, TIM1_CH2 = PC7)
 *
 *  The tone frequency is set through TIM1 PSC/ARR, the loudness through the
 *  PWM duty cycle of the same channel.
 *******************************************************************************/
#ifndef __BUZZER_H
#define __BUZZER_H

#include "ch32v00x.h"

/* Buzzer control pin. */
#define BUZZER_GPIO_PORT            GPIOC
#define BUZZER_GPIO_PIN             GPIO_Pin_7

/* Audible range that is clamped to. */
#define BUZZER_FREQ_MIN             20U
#define BUZZER_FREQ_MAX             20000U

/* volume is 0..100.  0 is silent, 100 is the loudest setting.
 * A passive buzzer radiates most energy at 50 % duty, so the volume is
 * mapped onto 0..50 % duty:
 *      volume 100 -> 50 % duty (loudest)
 *      volume  50 -> 25 % duty
 *      volume   0 -> output held low
 * Change BUZZER_DUTY_FULL_SCALE to 100 if you want duty = volume (0..100 %,
 * which however goes silent at volume 100). */
#define BUZZER_DUTY_FULL_SCALE      200U

/*********************************************************************
 * @fn      Buzzer_Init
 *
 * @brief   Configure PC7 as TIM1_CH2 PWM output, silent (duty 0).
 *          Call once after SystemCoreClockUpdate().
 *
 * @return  none
 */
void    Buzzer_Init(void);

/*********************************************************************
 * @fn      Buzzer_Start
 *
 * @brief   Start a continuous tone.
 *
 * @param   freq_hz - tone frequency in Hz, clamped to 20..20000.
 *          volume  - 0..100, 0 keeps the output low (silent).
 *
 * @return  none
 */
void    Buzzer_Start(uint16_t freq_hz, uint8_t volume);

/*********************************************************************
 * @fn      Buzzer_Stop
 *
 * @brief   Stop the tone (output held low).
 *
 * @return  none
 */
void    Buzzer_Stop(void);

/*********************************************************************
 * @fn      Buzzer_Beep
 *
 * @brief   Sound the buzzer for a given time.  Non blocking: the tone is
 *          started here and switched off by Buzzer_Task() once the time has
 *          elapsed, so that UART frame reception is never stalled by a long
 *          beep.
 *
 * @param   freq_hz     - tone frequency in Hz.
 *          volume      - 0..100.
 *          duration_ms - length of the beep in ms, 0 = stop immediately.
 *
 * @return  none
 */
void    Buzzer_Beep(uint16_t freq_hz, uint8_t volume, uint16_t duration_ms);

/*********************************************************************
 * @fn      Buzzer_Task
 *
 * @brief   Buzzer time base.  Must be called from the main loop about once
 *          per millisecond; it turns the tone off when the beep time is up.
 *
 * @return  none
 */
void    Buzzer_Task(void);

/*********************************************************************
 * @fn      Buzzer_IsBusy
 *
 * @brief   Tell whether a timed beep is still running.
 *
 * @return  1 while a beep is active, 0 otherwise
 */
uint8_t Buzzer_IsBusy(void);

#endif /* __BUZZER_H */
