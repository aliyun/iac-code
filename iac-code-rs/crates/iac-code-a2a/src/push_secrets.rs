use std::collections::BTreeMap;

use iac_code_protocol::json::JsonValue;

const AES_BLOCK_LEN: usize = 16;

mod envelope;
mod error;
mod fernet;
mod fs;
mod key_ids;
mod keyring;

pub use envelope::A2APushSecretEnvelope;
pub use error::A2APushSecretError;
pub use fernet::{fernet_decrypt, fernet_encrypt_at};
pub use keyring::A2APushSecretKeyring;

fn json_string(object: &BTreeMap<String, JsonValue>, key: &str) -> Option<String> {
    match object.get(key) {
        Some(JsonValue::String(value)) => Some(value.clone()),
        _ => None,
    }
}

fn aes_128_cbc_encrypt(
    key: &[u8; AES_BLOCK_LEN],
    iv: &[u8; AES_BLOCK_LEN],
    plaintext: &[u8],
) -> Vec<u8> {
    let round_keys = expand_aes_128_key(key);
    let mut padded = plaintext.to_vec();
    let padding_len = AES_BLOCK_LEN - (padded.len() % AES_BLOCK_LEN);
    padded.extend(std::iter::repeat_n(padding_len as u8, padding_len));

    let mut previous = *iv;
    let mut ciphertext = Vec::with_capacity(padded.len());
    for chunk in padded.chunks_exact(AES_BLOCK_LEN) {
        let mut block: [u8; AES_BLOCK_LEN] = chunk.try_into().expect("chunk size is fixed");
        xor_block(&mut block, &previous);
        aes_encrypt_block(&mut block, &round_keys);
        ciphertext.extend_from_slice(&block);
        previous = block;
    }
    ciphertext
}

fn aes_128_cbc_decrypt(
    key: &[u8; AES_BLOCK_LEN],
    iv: &[u8; AES_BLOCK_LEN],
    ciphertext: &[u8],
) -> Result<Vec<u8>, ()> {
    if ciphertext.is_empty() || !ciphertext.len().is_multiple_of(AES_BLOCK_LEN) {
        return Err(());
    }
    let round_keys = expand_aes_128_key(key);
    let mut previous = *iv;
    let mut plaintext = Vec::with_capacity(ciphertext.len());
    for chunk in ciphertext.chunks_exact(AES_BLOCK_LEN) {
        let cipher_block: [u8; AES_BLOCK_LEN] = chunk.try_into().expect("chunk size is fixed");
        let mut block = cipher_block;
        aes_decrypt_block(&mut block, &round_keys);
        xor_block(&mut block, &previous);
        plaintext.extend_from_slice(&block);
        previous = cipher_block;
    }
    remove_pkcs7_padding(&mut plaintext)?;
    Ok(plaintext)
}

fn remove_pkcs7_padding(value: &mut Vec<u8>) -> Result<(), ()> {
    let Some(&padding_len) = value.last() else {
        return Err(());
    };
    let padding_len = padding_len as usize;
    if padding_len == 0 || padding_len > AES_BLOCK_LEN || padding_len > value.len() {
        return Err(());
    }
    if !value[value.len() - padding_len..]
        .iter()
        .all(|byte| *byte as usize == padding_len)
    {
        return Err(());
    }
    value.truncate(value.len() - padding_len);
    Ok(())
}

fn xor_block(block: &mut [u8; AES_BLOCK_LEN], previous: &[u8; AES_BLOCK_LEN]) {
    for index in 0..AES_BLOCK_LEN {
        block[index] ^= previous[index];
    }
}

fn expand_aes_128_key(key: &[u8; AES_BLOCK_LEN]) -> [u8; 176] {
    let mut round_keys = [0_u8; 176];
    round_keys[..AES_BLOCK_LEN].copy_from_slice(key);
    let mut bytes_generated = AES_BLOCK_LEN;
    let mut rcon_index = 1;
    let mut temp = [0_u8; 4];

    while bytes_generated < round_keys.len() {
        temp.copy_from_slice(&round_keys[bytes_generated - 4..bytes_generated]);
        if bytes_generated.is_multiple_of(AES_BLOCK_LEN) {
            temp.rotate_left(1);
            for byte in &mut temp {
                *byte = S_BOX[*byte as usize];
            }
            temp[0] ^= RCON[rcon_index];
            rcon_index += 1;
        }

        for byte in temp {
            round_keys[bytes_generated] = round_keys[bytes_generated - AES_BLOCK_LEN] ^ byte;
            bytes_generated += 1;
        }
    }
    round_keys
}

fn aes_encrypt_block(block: &mut [u8; AES_BLOCK_LEN], round_keys: &[u8; 176]) {
    add_round_key(block, &round_keys[0..AES_BLOCK_LEN]);
    for round in 1..10 {
        sub_bytes(block);
        shift_rows(block);
        mix_columns(block);
        add_round_key(
            block,
            &round_keys[round * AES_BLOCK_LEN..(round + 1) * AES_BLOCK_LEN],
        );
    }
    sub_bytes(block);
    shift_rows(block);
    add_round_key(block, &round_keys[160..176]);
}

fn aes_decrypt_block(block: &mut [u8; AES_BLOCK_LEN], round_keys: &[u8; 176]) {
    add_round_key(block, &round_keys[160..176]);
    for round in (1..10).rev() {
        inv_shift_rows(block);
        inv_sub_bytes(block);
        add_round_key(
            block,
            &round_keys[round * AES_BLOCK_LEN..(round + 1) * AES_BLOCK_LEN],
        );
        inv_mix_columns(block);
    }
    inv_shift_rows(block);
    inv_sub_bytes(block);
    add_round_key(block, &round_keys[0..AES_BLOCK_LEN]);
}

fn add_round_key(block: &mut [u8; AES_BLOCK_LEN], round_key: &[u8]) {
    for (byte, key_byte) in block.iter_mut().zip(round_key) {
        *byte ^= *key_byte;
    }
}

fn sub_bytes(block: &mut [u8; AES_BLOCK_LEN]) {
    for byte in block {
        *byte = S_BOX[*byte as usize];
    }
}

fn inv_sub_bytes(block: &mut [u8; AES_BLOCK_LEN]) {
    for byte in block {
        *byte = INV_S_BOX[*byte as usize];
    }
}

fn shift_rows(block: &mut [u8; AES_BLOCK_LEN]) {
    let original = *block;
    block[0] = original[0];
    block[4] = original[4];
    block[8] = original[8];
    block[12] = original[12];
    block[1] = original[5];
    block[5] = original[9];
    block[9] = original[13];
    block[13] = original[1];
    block[2] = original[10];
    block[6] = original[14];
    block[10] = original[2];
    block[14] = original[6];
    block[3] = original[15];
    block[7] = original[3];
    block[11] = original[7];
    block[15] = original[11];
}

fn inv_shift_rows(block: &mut [u8; AES_BLOCK_LEN]) {
    let original = *block;
    block[0] = original[0];
    block[4] = original[4];
    block[8] = original[8];
    block[12] = original[12];
    block[1] = original[13];
    block[5] = original[1];
    block[9] = original[5];
    block[13] = original[9];
    block[2] = original[10];
    block[6] = original[14];
    block[10] = original[2];
    block[14] = original[6];
    block[3] = original[7];
    block[7] = original[11];
    block[11] = original[15];
    block[15] = original[3];
}

fn mix_columns(block: &mut [u8; AES_BLOCK_LEN]) {
    for column in 0..4 {
        let offset = column * 4;
        let a0 = block[offset];
        let a1 = block[offset + 1];
        let a2 = block[offset + 2];
        let a3 = block[offset + 3];
        let t = a0 ^ a1 ^ a2 ^ a3;
        block[offset] ^= t ^ xtime(a0 ^ a1);
        block[offset + 1] ^= t ^ xtime(a1 ^ a2);
        block[offset + 2] ^= t ^ xtime(a2 ^ a3);
        block[offset + 3] ^= t ^ xtime(a3 ^ a0);
    }
}

fn inv_mix_columns(block: &mut [u8; AES_BLOCK_LEN]) {
    for column in 0..4 {
        let offset = column * 4;
        let a0 = block[offset];
        let a1 = block[offset + 1];
        let a2 = block[offset + 2];
        let a3 = block[offset + 3];
        block[offset] = gf_mul(a0, 14) ^ gf_mul(a1, 11) ^ gf_mul(a2, 13) ^ gf_mul(a3, 9);
        block[offset + 1] = gf_mul(a0, 9) ^ gf_mul(a1, 14) ^ gf_mul(a2, 11) ^ gf_mul(a3, 13);
        block[offset + 2] = gf_mul(a0, 13) ^ gf_mul(a1, 9) ^ gf_mul(a2, 14) ^ gf_mul(a3, 11);
        block[offset + 3] = gf_mul(a0, 11) ^ gf_mul(a1, 13) ^ gf_mul(a2, 9) ^ gf_mul(a3, 14);
    }
}

fn xtime(value: u8) -> u8 {
    if value & 0x80 == 0 {
        value << 1
    } else {
        (value << 1) ^ 0x1b
    }
}

fn gf_mul(mut value: u8, mut multiplier: u8) -> u8 {
    let mut result = 0;
    while multiplier != 0 {
        if multiplier & 1 != 0 {
            result ^= value;
        }
        value = xtime(value);
        multiplier >>= 1;
    }
    result
}

const RCON: [u8; 11] = [
    0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36,
];

const S_BOX: [u8; 256] = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
];

const INV_S_BOX: [u8; 256] = [
    0x52, 0x09, 0x6a, 0xd5, 0x30, 0x36, 0xa5, 0x38, 0xbf, 0x40, 0xa3, 0x9e, 0x81, 0xf3, 0xd7, 0xfb,
    0x7c, 0xe3, 0x39, 0x82, 0x9b, 0x2f, 0xff, 0x87, 0x34, 0x8e, 0x43, 0x44, 0xc4, 0xde, 0xe9, 0xcb,
    0x54, 0x7b, 0x94, 0x32, 0xa6, 0xc2, 0x23, 0x3d, 0xee, 0x4c, 0x95, 0x0b, 0x42, 0xfa, 0xc3, 0x4e,
    0x08, 0x2e, 0xa1, 0x66, 0x28, 0xd9, 0x24, 0xb2, 0x76, 0x5b, 0xa2, 0x49, 0x6d, 0x8b, 0xd1, 0x25,
    0x72, 0xf8, 0xf6, 0x64, 0x86, 0x68, 0x98, 0x16, 0xd4, 0xa4, 0x5c, 0xcc, 0x5d, 0x65, 0xb6, 0x92,
    0x6c, 0x70, 0x48, 0x50, 0xfd, 0xed, 0xb9, 0xda, 0x5e, 0x15, 0x46, 0x57, 0xa7, 0x8d, 0x9d, 0x84,
    0x90, 0xd8, 0xab, 0x00, 0x8c, 0xbc, 0xd3, 0x0a, 0xf7, 0xe4, 0x58, 0x05, 0xb8, 0xb3, 0x45, 0x06,
    0xd0, 0x2c, 0x1e, 0x8f, 0xca, 0x3f, 0x0f, 0x02, 0xc1, 0xaf, 0xbd, 0x03, 0x01, 0x13, 0x8a, 0x6b,
    0x3a, 0x91, 0x11, 0x41, 0x4f, 0x67, 0xdc, 0xea, 0x97, 0xf2, 0xcf, 0xce, 0xf0, 0xb4, 0xe6, 0x73,
    0x96, 0xac, 0x74, 0x22, 0xe7, 0xad, 0x35, 0x85, 0xe2, 0xf9, 0x37, 0xe8, 0x1c, 0x75, 0xdf, 0x6e,
    0x47, 0xf1, 0x1a, 0x71, 0x1d, 0x29, 0xc5, 0x89, 0x6f, 0xb7, 0x62, 0x0e, 0xaa, 0x18, 0xbe, 0x1b,
    0xfc, 0x56, 0x3e, 0x4b, 0xc6, 0xd2, 0x79, 0x20, 0x9a, 0xdb, 0xc0, 0xfe, 0x78, 0xcd, 0x5a, 0xf4,
    0x1f, 0xdd, 0xa8, 0x33, 0x88, 0x07, 0xc7, 0x31, 0xb1, 0x12, 0x10, 0x59, 0x27, 0x80, 0xec, 0x5f,
    0x60, 0x51, 0x7f, 0xa9, 0x19, 0xb5, 0x4a, 0x0d, 0x2d, 0xe5, 0x7a, 0x9f, 0x93, 0xc9, 0x9c, 0xef,
    0xa0, 0xe0, 0x3b, 0x4d, 0xae, 0x2a, 0xf5, 0xb0, 0xc8, 0xeb, 0xbb, 0x3c, 0x83, 0x53, 0x99, 0x61,
    0x17, 0x2b, 0x04, 0x7e, 0xba, 0x77, 0xd6, 0x26, 0xe1, 0x69, 0x14, 0x63, 0x55, 0x21, 0x0c, 0x7d,
];
