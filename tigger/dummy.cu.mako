// A_prime -> A
${store.s1}(${load.l1});

// A_prime, B_prime, B_param -> B
${ctype.s1} t = ${mul(dtype.s1, dtype.l1)}(${load.p1}, ${load.l1});
${store.s1}(t + ${load.l2});

// B_new_prime -> B_prime
${store.s1}(${load.l1});

// C -> C_half1, C_half2
${ctype.s1} t = ${mul(dtype.l1, float32)}(${load.l1}, 0.5);
${store.s1}(t);
${store.s2}(t);

// C_half1 -> C_new_half1
${store.s1}(${load.l1});


KERNEL void dummy(${signature})
{
    int idx = GLOBAL_INDEX;
    ${ctype.A} a = ${load.A}(idx);
    ${ctype.B} b = ${load.B}(idx);
    ${ctype.C} c = a + ${mul(dtype.coeff, dtype.B)}(${param.coeff}, b);
    ${store.C}(idx, c);
}


float mul_float_float(float x, float y) { return x * y; }
float mul_int_float(float x, float y) { return x * y; }

// Connections from {load.A}
#define _LOAD_A_prime (A_prime[idx])

float _load_A(float *A_prime, int idx)
{
    return _LOAD_A_prime;
}
#define _LOAD_A(idx) _load_A(A_prime, idx)

// Connections from {load.B}
#define _LOAD_B_new_prime (B_new_prime[idx])

float load_B_new_prime(float *B_new_prime, int idx)
{
    return _LOAD_B_new_prime;
}
#define _LOAD_B_prime (_load_B_prime(B_new_prime, idx))

float _load_B(float *A_prime, float *B_new_prime, int B_param, int idx)
{
    float t = mul_float_float(B_param, _LOAD_A_prime;
    return (t + _LOAD_B_prime);
}
#define _LOAD_B(idx) _load_B(A_prime, B_new_prime, B_param, idx)

// Connections from {store.C}
#define _STORE_C_new_half1(val) C_new_half1[idx] = (val)

void _store_C_half1(float *C_new_half1, int idx, float val)
{
    _STORE_C_new_half1(val);
}
#define _STORE_C_half1(val) _store_C_half1(C_new_half1, idx, val)

#define _STORE_C_half2(val) C_half2[idx] = (val)

void _store_C(float *C_new_half1, float *C_half2, int idx, float val)
{
    float t = mul_float_float(val, 0.5);
    _STORE_C_half1(t);
    _STORE_C_half2(t);
}
#define _STORE_C(idx, c) _store_C(C_half1, C_half2, idx, c)

KERNEL void dummy(
    GLOBAL_MEM float *C_half1, GLOBAL_MEM float *C_half2,
    GLOBAL_MEM float *A_prime, GLOBAL_MEM float *B_prime,
    int coeff, int B_param
    )
{
    int idx = GLOBAL_INDEX;
    float a = _LOAD_A(idx);
    float b = _LOAD_B(idx);
    float c = a + mul_int_float(coeff, b);
    _STORE_C(idx, c);
}