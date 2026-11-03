/* Our code in free flash after the stock app, our variables in RAM the stock app never uses.
 * 0x08016800..0x08018000 is free on both WB32F3G71 variants (96 KB x9 / 128 KB xB). */
MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08016800, LENGTH = 0x1800
    RAM   (rwx) : ORIGIN = 0x20005000, LENGTH = 0x1000
}

SECTIONS
{
    .text :
    {
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    } > FLASH

    .data :
    {
        __data_start = .;
        *(.data*)
        . = ALIGN(4);
        __data_end = .;
    } > RAM AT > FLASH
    __data_load = LOADADDR(.data);

    .bss (NOLOAD) :
    {
        __bss_start = .;
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > RAM

    /DISCARD/ : { *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
