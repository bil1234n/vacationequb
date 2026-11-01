from rest_framework import serializers

from .models import PaymentTransaction


class PaymentTransactionSerializer(serializers.ModelSerializer):
    client_payload = serializers.SerializerMethodField()

    class Meta:
        model = PaymentTransaction
        fields = ["reference", "provider", "purpose", "amount", "currency", "status", "checkout_url",
                  "provider_reference", "failure_reason", "client_payload", "created_at", "completed_at"]
        read_only_fields = fields

    def get_client_payload(self, obj) -> dict:
        return (obj.raw_request or {}).get("client_payload", {})


class PayRequestSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=PaymentTransaction.Provider.choices, required=False)
    payer_phone = serializers.CharField(required=False, allow_blank=True, max_length=16)
