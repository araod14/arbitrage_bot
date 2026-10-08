# Plan: vigencia de oportunidades y fondos por método de pago

## Estado de implementación

Las cuatro entregas están implementadas. El monitor publica libros por par y
episodios de rutas; el dashboard ofrece mercado reciente, comprobaciones,
cuentas privadas, reservas y recomendaciones según los fondos disponibles.
Las tablas nuevas se crean al usar cada componente y las operaciones antiguas
reciben columnas opcionales para conservar la referencia a una comprobación.

Se conserva el seguimiento de las rutas activas dentro del límite configurado;
las nuevas ocupan huecos. Reducir ese límite pausa seguimientos explícitamente.
Las consultas idénticas refrescan fechas sin duplicar snapshots. Las unidades
se redondean hacia abajo a ocho decimales y una identidad ambigua no se confirma.
La pantalla privada muestra métricas de consultas y permite registrar resultados
con la versión de fondos y la comprobación utilizadas. Ver README para uso y
`.env.example` para los parámetros nuevos.

## Objetivo y alcance

Permitir que el usuario distinga una oportunidad histórica de una observación
reciente y calcule cuánto puede operar con los fondos disponibles en cada banco.
El producto sigue siendo un monitor: la ejecución y la actualización de saldos
son manuales.

Primera versión para el operador único que ya contempla el dashboard. Incluye
saldos fiat, métodos habilitados para recibir pagos, reservas manuales y
revalidación bajo demanda. La cartera cripto, las transferencias entre bancos y
la conciliación automática quedan para una ampliación posterior. Comprar y luego
vender no requiere disponer previamente de cripto, pero tampoco garantiza que la
venta siga disponible después de completar la compra.

## Decisiones de producto

### Vigencia

- Separar las vistas **Mercado reciente** e **Historial**.
- Mostrar primera observación, última comprobación exitosa, última observación
  favorable y antigüedad de los datos, con tiempos almacenados en UTC.
- Estados: **observada**, **revalidada**, **dejó de cumplir** y
  **sin datos recientes**. Revalidada significa que una consulta solicitada por
  el usuario encontró condiciones compatibles; no reserva anuncios.
- Configurar una ventana de frescura. Propuesta inicial: dos intervalos de
  barrido; calcular la antigüedad por par, incluso si el bot se detiene.
- Un error de red conserva el último resultado y lo marca desactualizado cuando
  corresponda. No significa que la oportunidad desapareció.
- No encontrar un anuncio en las filas consultadas significa **no observado**,
  no expiración confirmada. Una sustitución de contraparte se muestra como una
  nueva alternativa, sin confirmar el anuncio original.
- Agrupar por par, métodos y contrapartes, con episodios separados cuando se
  observa una interrupción. Conservar los cambios de precio como observaciones.
  Usar identidad de contraparte y evidencias adicionales: los `advNo` actuales
  no ofrecen identidad exacta fiable por sí solos.

### Fondos por método

- Cada cuenta tiene nombre, moneda, saldo declarado, fecha de actualización,
  métodos asociados y autorización para recibir pagos.
- Una cuenta puede respaldar varios métodos: su saldo se cuenta una sola vez.
- Disponible = saldo declarado − reservas activas. Rechazar negativos,
  cantidades no finitas y reservas superiores al disponible.
- El máximo por operación sigue limitado por `MAX_FIAT`, además del saldo de
  la cuenta de compra y los límites e inventario de ambas contrapartes.
- El saldo del banco receptor no limita una venta; importa que el usuario pueda
  recibir por ese método. No asumir transferencias inmediatas entre cuentas.
- Si falta configuración, mostrar **fondos sin configurar**; no asumir saldo
  cero ni presentar el fondo global como saldo bancario confirmado.
- Las recomendaciones compiten por el mismo capital: mostrarlas no lo reserva.
  Reservar es una acción explícita y liberar requiere otra acción explícita.
- Saldos, reservas, déficits y montos personalizados se muestran solo con login.
  La supervisión pública mantiene información de mercado independiente.

## Arquitectura propuesta

Mantener funciones puras, `Decimal` y modelos inmutables en `domain/`.
Incorporar modelos de observación, cuenta, reserva y evaluación de capacidad.
Los casos de uso y sus `Protocol` viven en `application/`; los adaptadores del
monitor se conectan en `main.py`.

El bot conserva la propiedad de la base de oportunidades. Añadir tablas de
observaciones/episodios y un snapshot acotado del libro por par. El dashboard
las lee sin migrarlas. Mantener los snapshots históricos usados por operaciones
registradas, aunque se actualicen las condiciones recientes.

Guardar cuentas y reservas en la base privada del dashboard, separada de la
base del bot. La evaluación personalizada se calcula en el servidor con el libro
reciente y los saldos privados: no publicar sus resultados en `status.json`.

Para revalidar, añadir una cola de solicitudes en una base de control separada:
el dashboard crea solicitudes autenticadas y el bot las procesa con su fuente
de mercado existente. Usar transacciones breves, WAL, identificadores únicos,
caducidad y estados pendiente/en proceso/completada/error. Los resultados
personalizados permanecen privados. Limitar solicitudes repetidas y compartir
concurrencia y backoff con el monitor. Un comando pendiente tras reiniciar solo
se recupera si sigue vigente. Con el bot detenido, explicar que debe arrancarse.

## Implementación por entregas

### 1. Observaciones y frescura

Modificar `application/monitor.py` para actualizar las observaciones en cada
ciclo exitoso, antes de suprimir avisos duplicados. Distinguir una consulta
fallida de un libro válido sin pares elegibles y registrar el estado por par.

Persistir los anuncios necesarios para reevaluar rutas. No inferir que una ruta
dejó de cumplir solo porque otra pasó a ser la mejor: comprobar las rutas
seguidas contra el libro consultado. Acotar retención y número de rutas activas.

Añadir migraciones en `infrastructure/sqlite_repo.py`, lectura compatible con
esquemas anteriores en `web/db_reader.py` y estados en las plantillas. Las filas
antiguas siguen en el historial y no se presentan como revalidadas.

**Aceptación:** una oportunidad repetida actualiza su frescura sin duplicar
avisos; un error no la marca como perdida; detener el bot termina mostrando
datos desactualizados; cambiar de mejor oportunidad no invalida otras rutas.

### 2. Revalidación manual

Añadir el caso de uso, los puertos y el adaptador de cola; incorporar su
procesamiento al bucle del bot. Añadir un POST autenticado para solicitar la
comprobación y un parcial HTMX autenticado para consultar el resultado.

La consulta revisa compra y venta, límites, inventario y umbral con una versión
identificada de configuración. Mostrar precios anteriores y nuevos, hora de
comprobación y motivo de incompatibilidad. No cambiar el snapshot histórico.

**Aceptación:** éxito, margen insuficiente, contraparte no observada, timeout,
bot detenido y solicitudes repetidas tienen respuestas distintas y claras.

### 3. Cuentas y reservas privadas

Crear almacenamiento privado y pantalla **Fondos** para registrar/editar
cuentas, asociar métodos, declarar saldos y reservar/liberar importes.
Registrar cambios y su fecha. Archivar cuentas con historial en lugar de borrar
sus movimientos. Resolver reservas concurrentes en una transacción e impedir
que dos solicitudes reserven el mismo disponible.

No modificar saldos automáticamente al registrar una operación: ofrecer una
actualización manual explícita para evitar descontar dos veces.

**Aceptación:** los saldos persisten tras reiniciar, los métodos compartidos no
duplican capital y los visitantes públicos no pueden leerlos ni modificarlos.

### 4. Oportunidades compatibles con los fondos

Extender el cálculo del dominio para evaluar cantidades variables. Para cada
par de anuncios, obtener el intervalo de unidades permitido por mínimos,
máximos e inventario de ambos lados, y acotarlo por saldo y `MAX_FIAT`.
Si el intervalo es válido, calcular el monto y la ganancia para esa cantidad.

Este cálculo debe partir de candidatos del libro, no solo de la mejor
oportunidad guardada: actualmente el motor descarta anuncios que no aceptan el
fondo completo, aunque permitirían una operación menor.

Mostrar **compatible**, **saldo insuficiente**, **método receptor no habilitado**,
**límites incompatibles**, **fondos sin configurar** o **mercado desactualizado**.
Ordenar los resultados compatibles por ganancia estimada en fiat y permitir
ver otras rutas. Si cambian los saldos durante la revalidación, recalcular con
su nueva versión antes de ofrecer una reserva.

**Aceptación:** con 5.000 VES disponibles puede recomendar una compra menor
que `MAX_FIAT` cuando los anuncios lo permiten; si los mínimos exigen 6.000 VES,
explica el déficit. El mismo dinero no se suma entre métodos ni criptomonedas.

## Validación y documentación

- Dominio: fronteras de mínimos/máximos, inventario de venta para las unidades
  compradas, reservas, métodos compartidos y cantidades inválidas.
- Monitor: reloj y mercado falsos, duplicados, errores, varias rutas, distintos
  pares y revalidación; ninguna llamada real a Binance.
- Persistencia: migraciones, recuperación de solicitudes y reservas concurrentes
  usando SQLite en `tmp_path`.
- Web: `TestClient` en `tests/test_web_endpoints.py`, `ENV_PATH` temporal vacío,
  autenticación, ausencia de datos privados en respuestas públicas y ningún
  subproceso real del bot.
- Ejecutar pruebas enfocadas por entrega y `make test` antes de integrar.
- Actualizar README y `.env.example` para rutas, retención, frescura y límites
  de solicitudes; incluir nuevas plantillas en los assets empaquetados.

## Orden y medición

Entregar 1 → 2 → 3 → 4; cada etapa añade comportamiento utilizable.
Medir tiempo de revalidación, proporción de consultas que conservan condiciones
favorables y frecuencia de incompatibilidades por fondos o límites. Registrar
la versión de condiciones revalidadas junto al intento manual permite conectar
estas mejoras con los resultados reales, sin contar detecciones repetidas como
ganancias acumulables.
