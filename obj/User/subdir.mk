################################################################################
# MRS Version: 2.5.0
# Automatically-generated file. Do not edit!
################################################################################

# Add inputs and outputs from these tool invocations to the build variables 
C_SRCS += \
../User/buzzer.c \
../User/ch32v00x_it.c \
../User/main.c \
../User/protocol.c \
../User/system_ch32v00x.c \
../User/uart.c \
../User/ws2812.c 

C_DEPS += \
./User/buzzer.d \
./User/ch32v00x_it.d \
./User/main.d \
./User/protocol.d \
./User/system_ch32v00x.d \
./User/uart.d \
./User/ws2812.d 

OBJS += \
./User/buzzer.o \
./User/ch32v00x_it.o \
./User/main.o \
./User/protocol.o \
./User/system_ch32v00x.o \
./User/uart.o \
./User/ws2812.o 

DIR_OBJS += \
./User/*.o \

DIR_DEPS += \
./User/*.d \

DIR_EXPANDS += \
./User/*.234r.expand \


# Each subdirectory must supply rules for building sources it contributes
User/%.o: ../User/%.c
	@	riscv-none-embed-gcc -march=rv32ecxw -mabi=ilp32e -msmall-data-limit=0 -msave-restore -fmax-errors=20 -Os -fmessage-length=0 -fsigned-char -ffunction-sections -fdata-sections -fno-common -Wunused -Wuninitialized -g -I"d:/Desktop/文件夹/Microcomputer/AI_Status_Lights/Project/AIStatusLights/Debug" -I"d:/Desktop/文件夹/Microcomputer/AI_Status_Lights/Project/AIStatusLights/Core" -I"d:/Desktop/文件夹/Microcomputer/AI_Status_Lights/Project/AIStatusLights/User" -I"d:/Desktop/文件夹/Microcomputer/AI_Status_Lights/Project/AIStatusLights/Peripheral/inc" -std=gnu99 -MMD -MP -MF"$(@:%.o=%.d)" -MT"$(@)" -c -o "$@" "$<"

