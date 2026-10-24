"""Which fault toggles can be set back to their resting value for which container. Data only.

emitRawPii and cartMetricCardinalityOverflow are not failure scenarios; the aiRunawayAgent and aiSlowResponse
toggles belong to a service that is not in this compose project. The restart deny list in registry.py does
not apply to this table.
"""
RESTING_VARIANT = "off"

REMEDIATIONS: dict[str, tuple[str, ...]] = {
    "ad": ("adFailure", "adHighCpu", "adManualGc"),
    "cart": ("cartFailure", "failedReadinessProbe"),
    "email": ("emailMemoryLeak",),
    "payment": ("paymentFailure", "paymentUnreachable"),
    "product-catalog": ("productCatalogFailure", "productCatalogLockContention"),
    "astronomy-db": ("productCatalogLockContention",),
    "recommendation": ("recommendationCacheFailure",),
    "shipping": ("intlShippingSlowdown",),
    "image-provider": ("imageSlowLoad",),
    "frontend": ("imageSlowLoad", "loadGeneratorFloodHomepage"),
    "load-generator": ("loadGeneratorFloodHomepage",),
    "kafka": ("kafkaConsumerDead", "kafkaQueueProblems"),
    "fraud-detection": ("kafkaConsumerDead", "kafkaQueueProblems"),
}
