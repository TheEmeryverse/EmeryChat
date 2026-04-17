function isValidPassword(password) {
    const result = {
        isValid: true,
        errors: []
    };
    
    if (!password || typeof password !== 'string') {
        result.isValid = false;
        result.errors.push('Password is required and must be a string')
        return result;
    }
    
    if (password.length < 8) {
        result.isValid = false;
        result.errors.push('Password must be at least 8 characters long')
    }
    
    if (password.length >= 32) {
        result.isValid = false;
        result.errors.push('Password must be no more than 32 characters long')
    }
    
    if (!/[A-Z]/.test(password)) {
        result.isValid = false;
        result.errors.push('Password must contain at least one uppercase letter')
    }
    
    if (!/\d/.test(password)) {
        result.isValid = false;
        result.errors.push('Password must contain at least one number')
    }
    
    return result
}

function isValidUserName(str) {
    const result = {
        isValid: true,
        errors: []
    }

    // Check if string is empty or not a string
    if (!str || typeof str !== 'string') {
        result.isValid = false
        result.errors.push('Invalid Username')
    }

    if(str.length < 2) {
        result.isValid = false
        result.errors.push('Username too short')
    }

    if(str.length >= 32) {
        result.isValid = false
        result.errors.push('Username too long')
    }
    
    // Check if string has consecutive spaces
    if (str.includes('  ')) {
        result.isValid = false
        result.errors.push('Cannot have consecutive spaces')
    }
    
    // Check if all characters are either letters, numbers, or spaces
    for (let i = 0; i < str.length; i++) {
        const char = str[i]
        if (!/[a-zA-Z0-9 ]/.test(char)) {
            result.isValid = false
            result.errors.push('Only letters, numbers, and spaces')
        }
    }
    
    return result
}

module.exports = {
    isValidPassword,
    isValidUserName
}