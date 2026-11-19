# Protocolo congelado de ablación de `SRTO_OHEADBW`

Estado: `frozen_before_execution`.

Este protocolo se escribe antes de producir datos. La campaña es una ablación
explicativa posterior al contraste confirmatorio V2. No reemplaza ni recalcula
el efecto ya observado entre la capa asistida y LiveCC sin asistencia.

## Pregunta y alcance

La campaña busca distinguir si la mejora de la entrega útil depende de adaptar
temporalmente `SRTO_OHEADBW` o si puede obtenerse manteniendo el valor de 10 %
durante toda la transmisión. Su alcance queda limitado al perfil CBR y a la
transición de capacidad ya estudiados.

La comparación primaria es

`delta_static = mean(U_assistant) - mean(U_const10)`.

La métrica se conserva sin modificaciones:

`U = byteRecvUniqueTotal / B_ref`, con `B_ref = 65.881.120` bytes.

La fuente debe aportar exactamente 63.749.672 bytes de payload en cada
corrida. El emisor y el receptor conservarán afinidad en los núcleos 0 y 1,
respectivamente.

## Identidad congelada

- Fuente de código de la evidencia: etiqueta `thesis-evidence-v2`, commit
  `2ea706bcb3ee728925f3364d914d2135a00e83e5`.
- Binario institucional esperado: SHA-256
  `b7bb5f337862322531d9ae8b2dffb4d475d6ae42d053191c12d6938992149734`.
- SRT: commit `c39196c9a568ae4e3289dd65cf54ba4154deb4a1`.
- Fuente MPEG-TS: SHA-256
  `35e43ccfca02e19b33cf1a2e8c2bf33a5bbe5970b8b8ef694eb9a21fecb12c99`.
- Configuración del controlador: SHA-256
  `36e7a68a9ee968f92a05af8dd6faea9c63949f9ca17e1a6a9a6da1a9fd7a66f4`.
- Selección del estimador: SHA-256
  `24f7fb4a2416ec46ba58eb91f365d1cb015fd7b7e0788ec70eeb252349a7011c`.

La ejecución debe usar el binario institucional mediante
`SRT_EXPERIMENT_BIN`. El script abortará antes de una corrida si su hash no
coincide. El commit de la rama de ablación identificará la nueva orquestación y
no se confundirá con el commit que produjo la campaña anterior.

## Perfil experimental

- fuente: MPEG-TS CBR de 8,5 Mbit/s;
- duración: 60 s;
- capacidad alta: `1,40R` durante 15 s;
- capacidad reducida: `1,04R` durante 30 s;
- recuperación: `1,40R` durante 15 s;
- retardo: 45 ms por sentido;
- pérdida: 1 %;
- reordenamiento: 0 %;
- TSBPD: 160 ms;
- muestreo: 100 ms;
- cola: 151 paquetes, con la fórmula histórica basada en `1,12R`.

No se modifican la fuente, el payload, el filtro, los umbrales, el horizonte,
la latencia, LiveCC ni el código C++.

## Brazos

1. `vanilla`: 25 % durante toda la corrida.
2. `const10`: 10 % durante toda la corrida.
3. `oracle10`: 25 %, 10 % durante la fase reducida y retorno a 25 % en la
   recuperación, mediante el calendario conocido.
4. `assistant`: controlador V2 congelado, con valor nominal de 25 % y valor
   activo de 10 %.

Se ejecutarán cien corridas válidas por brazo. El diseño es independiente porque
la semilla de `netem` no resultó reproducible. El orden se generará antes de la
campaña mediante cien bloques permutados de tamaño cuatro; cada bloque contiene
exactamente una corrida de cada brazo. La semilla del plan es `20261118`.

Se ejecutarán cinco controles limpios antes de la campaña, cinco al finalizar y
un control adicional cada cien trabajos experimentales completados. Estos
controles, la corrida de humo y las comprobaciones de equivalencia no forman
parte de las cuatrocientas corridas analizables.

## Contraste e interpretación

El margen de equivalencia se fija en `epsilon = 0,0003` unidades de `U`. Equivale
a 19.764 bytes por corrida y representa aproximadamente el 20 % del efecto medio
observado en la réplica institucional. El margen se define mediante evidencia
anterior e independiente de esta campaña.

Se calcularán:

- diferencia de medias `assistant - const10`;
- IC95 bootstrap no pareado con 100.000 remuestreos y semilla `20261119`;
- TOST de Welch con `alpha = 0,05` e IC90 para equivalencia;
- diferencia de medianas, probabilidad de superioridad, permutación y análisis
  por orden como comprobaciones de sensibilidad.

La decisión primaria sigue este orden:

1. superioridad práctica del asistente si el límite inferior del IC95 supera
   `+epsilon`;
2. superioridad práctica de `const10` si el límite superior del IC95 es menor
   que `-epsilon`;
3. equivalencia práctica si el IC90 de Welch queda contenido en
   `[-epsilon, +epsilon]`;
4. diferencia positiva o negativa de magnitud no determinada si el IC95 excluye
   cero, pero no se satisface una regla anterior;
5. resultado inconcluso en cualquier otro caso.

Las comparaciones `const10 - vanilla`, `oracle10 - vanilla`,
`assistant - oracle10` y `assistant - vanilla` son secundarias y descriptivas.
No sustituyen el contraste primario ni se utilizan para recalibrar el sistema.

## Integridad de una corrida

Una corrida es válida cuando:

- existen las tres metadata y las series esperadas;
- coinciden plan, brazo, modo, fuente, payload, duración, perfil, hashes y
  configuración;
- el payload se leyó y envió por completo;
- las series son finitas, sus contadores acumulativos son monótonos y el
  calendario de muestreo no presenta posiciones omitidas;
- `vanilla` tiene cero cambios y termina en 25 %;
- `const10` tiene cero cambios y termina en 10 %;
- `oracle10` tiene dos cambios en los límites efectivos de fase y termina en
  25 %;
- `assistant` alcanza `v2_ready=1`, termina en 25 % y tiene cero o dos cambios;
- cuando `assistant` tiene dos cambios, la activación fue confirmada, no ocurrió
  antes de la transición, retornó al nominal y `mbpsMaxBW` coincidió con el
  actuador;
- el replay Python/C++ del brazo asistido obtiene estado `equivalent`.

La falta de activación del asistente no invalida una corrida que alcanzó
`v2_ready=1`. Los descartes, tardíos, retransmisiones y crecimiento del buffer
son resultados y nunca criterios de exclusión.

Una corrida inválida se conserva, se registra y se reemplaza con un identificador
nuevo. No se sobrescriben directorios ni se aumenta el tamaño por observar un
resultado inconcluso.

## Métricas explicativas por fase

Para `pktRcvBelated`, que se registra por intervalo, se sumarán las muestras
correspondientes a cada fase. Para contadores acumulativos como
`pktRcvDropTotal` y `pktRetransTotal`, se calculará la diferencia entre los
límites efectivos registrados en `network_events.csv`.

Se describirán además `msSndBuf`, `byteSndBuf`, `mbpsSendRate`, `mbpsMaxBW` y
`pktSndDropTotal`. Ninguna métrica secundaria reemplaza a `U`.

## Resultado que limita la contribución

Si `const10` resulta equivalente o superior al asistente, el efecto histórico
frente a LiveCC permanece intacto, pero no se atribuirá una ventaja específica al
estimador dentro de este perfil. Si el asistente supera prácticamente a
`const10`, la campaña respaldará el valor adicional de la adaptación temporal.

## Enmienda 1 (anterior a los datos)

Fecha: 18 de noviembre de 2026. Protocolo base: commit `7685170`.

La revisión del auditor detectó que la formulación original mezclaba criterios
de integridad con respuestas producidas por el tratamiento. En particular, una
activación anterior a la fase reducida, la ausencia de restauración y una
cantidad distinta de cambios podían invalidar y reemplazar una corrida del
asistente. Esa regla habría condicionado la inclusión a un comportamiento
posterior a la asignación y, por lo tanto, podía favorecer el brazo asistido.

Desde esta enmienda, el contraste primario incorpora todas las corridas que
cumplan la integridad común, con independencia de cuándo actúe el controlador,
cuántos cambios produzca o si retorna al valor nominal antes de finalizar. Esas
conductas se registrarán como resultados. También se informará un análisis de
sensibilidad secundario que excluya las conductas irregulares, sin sustituir el
contraste principal.

La ventana de 3.000 a 10.000 ms se comprobará simétricamente en los cuatro
brazos. Debe contener al menos veinte muestras con RTT positivo. En el asistente,
`v2_ready=1` se utilizará como verificación cruzada. Si la serie resulta
calibrable y el binario informa lo contrario, la campaña se detendrá para
diagnóstico y la corrida no será reemplazada automáticamente.

La puntualidad de las transiciones de capacidad se comprobará en los cuatro
brazos con una tolerancia de 250 ms. Las exclusiones y reemplazos quedarán
limitados a fallas simétricas y predefinidas de archivos, identidad, payload,
calendario, configuración, contadores o instrumentación.

Una discrepancia entre el replay Python y C++ detendrá la campaña, pero nunca
excluirá por sí sola una observación de `U`. El tratamiento está constituido por
el binario C++ congelado. Si el diagnóstico atribuye la diferencia a la
referencia o al replay, la corrida permanecerá en el análisis primario y la
excepción se declarará al informar la equivalencia. Si se demuestra una falla
independiente de infraestructura, la validez se resolverá mediante los criterios
de integridad comunes y no por el resultado del replay.

Para reanudar después de una discrepancia atribuida a la referencia, se deberá
crear una disposición explícita con la decisión
`retain_primary_reference_failure`, una justificación y la identificación de la
corrida. El auditor conservará esa disposición junto al informe y contabilizará
la excepción. No se admite una disposición que excluya por sí sola la corrida.

Los fallos de ejecución o del propio auditor cuyo origen no esté demostrado
detendrán la campaña. No se reemplazarán hasta establecer que corresponden a
una causa de integridad independiente del tratamiento.

## Enmienda 2 (anterior a los datos)

Fecha: 19 de noviembre de 2026. Protocolo anterior: commit `cda175a`.
Implementación revisada: commit `a04b972`.

La reanudación de la campaña no podrá convertir por sí sola un fallo de
ejecución o del auditor en una corrida reemplazable. Después de cualquiera de
esos fallos, el trabajo permanecerá bloqueado hasta que exista una disposición
asociada al orden del plan, intento e identificador de corrida. La única decisión
admitida será `infrastructure_failure_replace`, acompañada de una justificación.
Una vez autorizada, el reemplazo utilizará un identificador nuevo y conservará
intactos el directorio y el log originales.

Si una línea base calibrable produce `v2_ready=0`, la campaña se detendrá. El
diagnóstico podrá conservar la corrida mediante la disposición
`retain_primary_controller_readiness_failure`. Esta decisión reconoce como
tratamiento al binario congelado: la falta de inicialización pasa a ser una
conducta observada, la corrida permanece en el contraste primario y la
equivalencia con el replay Python se declara no aplicable para ese caso.

Una decisión `set_ohead_error` detendrá la campaña como falla de aplicación del
actuador. No autorizará un reemplazo hasta que una disposición de infraestructura
documente la causa.

Las disposiciones de reemplazo se almacenarán en
`results/v2/ohead_ablation/failure_dispositions/plan_<orden>_attempt_<intento>.json`.
Las excepciones de inicialización del controlador se conservarán en
`results/v2/ohead_ablation/controller_dispositions/run_<id>.json`.

Los informes derivados de cada reauditoría se escribirán en un archivo temporal
y se promoverán de forma atómica. De este modo, una excepción no podrá hacer que
el orquestador interprete como actual un informe producido por una pasada
anterior.
