/********************************** (C) COPYRIGHT *******************************
 * File Name          : main.c
 * Description        : AI Status Lights - CH32V003F4P6 firmware.
 *
 *  Peripherals
 *  -----------
 *      PD3 -> 7 x WS2812B data line (TIM2_CH2 PWM + DMA1_Channel2)
 *      PC7 -> passive buzzer         (TIM1_CH2, TIM1 full remap)
 *      PD5 -> USART1_TX              (115200 8-N-1)
 *      PD6 -> USART1_RX
 *
 *  LED order: index 0 is the LED closest to the MCU.
 *
 *  UART protocol: see protocol.h
 *******************************************************************************/
#include "debug.h"
#include "ws2812.h"
#include "uart.h"
#include "buzzer.h"
#include "protocol.h"

/* Start-up colour pattern, one distinct colour per LED. */
static const uint8_t LedColor[WS2812_LED_NUM][3] = {
    { 255,   0,   0 },   /* LED1 - red     */
    { 255,  96,   0 },   /* LED2 - orange  */
    { 255, 255,   0 },   /* LED3 - yellow  */
    {   0, 255,   0 },   /* LED4 - green   */
    {   0, 255, 255 },   /* LED5 - cyan    */
    {   0,   0, 255 },   /* LED6 - blue    */
    { 160,   0, 255 },   /* LED7 - purple  */
};

/* Main loop period, also the time base of the buzzer. */
#define MAIN_LOOP_MS                1U

/* Rewrite the LED frame regularly, the WS2812B would hold its colours
 * without it, this only hardens the display against supply glitches. */
#define LED_REFRESH_PERIOD_MS       500U

/*********************************************************************
 * @fn      main
 *
 * @brief   Program entry point.
 *
 * @return  none
 */
int main(void)
{
    uint8_t  i;
    uint16_t refresh_timer = 0;

    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_1);
    SystemCoreClockUpdate();
    Delay_Init();

    /* PD5/PD6 USART1, receive interrupt enabled. */
    UART_Init(UART_DEFAULT_BAUD);

    printf("SystemClk:%d\r\n", SystemCoreClock);
    printf("ChipID:%08x\r\n", DBGMCU_GetCHIPID());
    printf("AI Status Lights: %d x WS2812B on PD3, buzzer on PC7, UART1 %d 8-N-1 (PD5/PD6)\r\n",
           WS2812_LED_NUM, UART_DEFAULT_BAUD);

    /* PD3 -> TIM2_CH2 PWM + DMA, 800 kHz WS2812B waveform. */
    WS2812_Init();
    /* PC7 -> TIM1_CH2 PWM, silent. */
    Buzzer_Init();
    Protocol_Init();

    /* Show the start-up pattern. */
    for(i = 0; i < WS2812_LED_NUM; i++)
    {
        WS2812_SetPixel(i, LedColor[i][0], LedColor[i][1], LedColor[i][2]);
    }

    if(WS2812_Refresh() == WS2812_OK)
    {
        printf("Frame sent\r\n");
    }
    else
    {
        printf("ERROR: WS2812 DMA transfer timed out\r\n");
    }

    while(1)
    {
        /* Handle every complete command frame received on USART1. */
        Protocol_Task();

        /* Buzzer time base, one tick per millisecond. */
        Buzzer_Task();

        /* Periodic re-send of the current LED colours. */
        refresh_timer++;
        if(refresh_timer >= LED_REFRESH_PERIOD_MS)
        {
            refresh_timer = 0;
            WS2812_Refresh();
        }

        Delay_Ms(MAIN_LOOP_MS);
    }
}
