package infraguard.rules.aliyun.oss_bucket_public_write_prohibited

import rego.v1

import data.infraguard.helpers

rule_meta := {
	"id": "oss-bucket-public-write-prohibited",
	"severity": "high",
	"name": {
		"en": "OSS Bucket Public Write Prohibited",
		"zh": "OSS 存储空间 ACL 不开启公共读写",
		"ja": "OSS バケットのパブリック書き込みが禁止",
		"de": "OSS-Bucket öffentliches Schreiben verboten",
		"es": "Escritura Pública de Bucket OSS Prohibida",
		"fr": "Écriture Publique de Bucket OSS Interdite",
		"pt": "Escrita Pública de Bucket OSS Proibida"
	},
	"description": {
		"en": "OSS buckets should not allow public write access. Public write access allows anyone to upload, modify, or delete objects in the bucket, which poses significant security risks.",
		"zh": "OSS 存储空间不应允许公共写入访问。公共写入访问允许任何人上传、修改或删除存储空间中的对象，这会带来重大安全风险。",
		"ja": "OSS バケットはパブリック書き込みアクセスを許可すべきではありません。パブリック書き込みアクセスにより、誰でもバケット内のオブジェクトをアップロード、変更、または削除でき、重大なセキュリティリスクをもたらします。",
		"de": "OSS-Buckets sollten keinen öffentlichen Schreibzugriff zulassen. Öffentlicher Schreibzugriff ermöglicht es jedem, Objekte im Bucket hochzuladen, zu ändern oder zu löschen, was erhebliche Sicherheitsrisiken birgt.",
		"es": "Los buckets OSS no deben permitir acceso de escritura público. El acceso de escritura público permite a cualquiera cargar, modificar o eliminar objetos en el bucket, lo que plantea riesgos de seguridad significativos.",
		"fr": "Les buckets OSS ne doivent pas autoriser l'accès en écriture public. L'accès en écriture public permet à quiconque de télécharger, modifier ou supprimer des objets dans le bucket, ce qui pose des risques de sécurité importants.",
		"pt": "Buckets OSS não devem permitir acesso de escrita público. O acesso de escrita público permite que qualquer pessoa faça upload, modifique ou exclua objetos no bucket, o que representa riscos significativos de segurança."
	},
	"reason": {
		"en": "The OSS bucket has public write access enabled (public-read-write ACL), which allows unauthorized users to modify or delete data.",
		"zh": "OSS 存储空间启用了公共写入访问（public-read-write ACL），允许未授权用户修改或删除数据。",
		"ja": "OSS バケットでパブリック書き込みアクセスが有効になっている（public-read-write ACL）ため、不正なユーザーがデータを変更または削除できます。",
		"de": "Der OSS-Bucket hat öffentlichen Schreibzugriff aktiviert (public-read-write ACL), was unbefugten Benutzern ermöglicht, Daten zu ändern oder zu löschen.",
		"es": "El bucket OSS tiene habilitado el acceso de escritura público (ACL public-read-write), lo que permite a usuarios no autorizados modificar o eliminar datos.",
		"fr": "Le bucket OSS a l'accès en écriture public activé (ACL public-read-write), ce qui permet aux utilisateurs non autorisés de modifier ou supprimer des données.",
		"pt": "O bucket OSS tem acesso de escrita público habilitado (ACL public-read-write), o que permite que usuários não autorizados modifiquem ou excluam dados."
	},
	"recommendation": {
		"en": "Change the bucket ACL to private or public-read by setting the AccessControl property to 'private' or 'public-read'.",
		"zh": "通过将 AccessControl 属性设置为'private'或'public-read'，将存储空间 ACL 更改为私有或公共读。",
		"ja": "AccessControl プロパティを 'private' または 'public-read' に設定して、バケット ACL をプライベートまたはパブリック読み取りに変更します。",
		"de": "Ändern Sie die Bucket-ACL auf privat oder öffentlich-lesen, indem Sie die AccessControl-Eigenschaft auf 'private' oder 'public-read' setzen.",
		"es": "Cambie el ACL del bucket a privado o lectura pública estableciendo la propiedad AccessControl en 'private' o 'public-read'.",
		"fr": "Modifiez l'ACL du bucket en privé ou lecture publique en définissant la propriété AccessControl sur 'private' ou 'public-read'.",
		"pt": "Altere o ACL do bucket para privado ou leitura pública definindo a propriedade AccessControl como 'private' ou 'public-read'."
	},
	"resource_types": ["ALIYUN::OSS::Bucket"]
}

deny contains result if {
	some name, resource in helpers.resources_by_type("ALIYUN::OSS::Bucket")
	is_public_write(resource)
	result := {
		"id": rule_meta.id,
		"resource_id": name,
		"violation_path": ["Properties", "AccessControl"],
		"meta": {
			"severity": rule_meta.severity,
			"reason": rule_meta.reason,
			"recommendation": rule_meta.recommendation,
		},
	}
}

is_public_write(resource) if {
	resource.Properties.AccessControl == "public-read-write"
}
