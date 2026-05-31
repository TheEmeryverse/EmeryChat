import * as React from 'react'
import {isValidPassword, isValidUserName} from '../common/helpers'

export const SignUp = () => {

    const [creds, updateCreds] = React.useState({
        password: '',
        passwordConfirm: '',
        name: '',
    })

    const [errors, updateErrors] = React.useState({
        password: '',
        passwordConfirm: '',
        name: '',
        general: ''
    })

    const inputText = (field, value) => {
        updateCreds({
            ...creds,
            [field]: value
        })
    }

    const signup = async (username, password, passwordConfirm) => {

        let nameErr = ''
        let passwordErr = ''
        let passwordConfirmErr = ''

        const name = username.trim()
        
        if (!name) {
            nameErr = 'Please enter a username'
        } else {
            const nameValidity = isValidUserName(name)
            if (!nameValidity.isValid) {
                nameErr = nameValidity.errors[0]
            }
        }

        if (!password) {
            passwordErr = 'Please enter your password'
        } else {
            const passwordValidity = isValidPassword(password)
            if (!passwordValidity.isValid) {
                passwordErr = passwordValidity.errors[0]
            }
        }

        if (!passwordConfirm) {
            passwordConfirmErr = 'Please re-enter your password'
        } else if (password !== passwordConfirm) {
            passwordConfirmErr = 'Passwords do not match'
        }

        if (nameErr || passwordErr || passwordConfirmErr) {
            updateErrors({
                ...updateErrors,
                name: nameErr,
                password: passwordErr,
                passwordConfirm: passwordConfirmErr
            })
            return
        }

        try {
            const headers = new Headers();
            headers.append("Content-Type", "application/json")
            const response = await fetch('http://localhost:3000/user/signup', {
                method: 'POST',
                body: JSON.stringify({
                    password,
                    name
                }),
                credentials: 'include',
                headers,
            })
            const body = await response.json()
            if (!body.success) {
                updateErrors({
                    ...updateErrors,
                    general: body.reason
                })
                return
            } else {
                window.location.href = body.redirect
                updateErrors({
                    password: '',
                    passwordConfirm: '',
                    name: '',
                    general: ''
                })
            }
        } catch (error) {
            console.log('failed to login: ', error)
        }
    }

    return <div className='loginContainer'>
        <div className='loginBox'>
            <h1>Create Account</h1>
            <form
                action="#"
                className='loginForm'
                onSubmit={(event) => {
                    event.preventDefault()
                    signup(creds.name, creds.password, creds.passwordConfirm)
                }}>

                <label htmlFor="name">Username</label><br />
                <input
                    type="text"
                    id="name"
                    name="name"
                    className={`textBox ${errors.name ? 'error' : ''}`}
                    placeholder='username'
                    onChange={(e) => {inputText('name', e.target.value)}}
                />
                {
                    errors.name ? <div className='errorText'>
                    {errors.name}
                    </div> : void 0
                }
                <br />
                <br />
                <hr/>
                <br />

                <label htmlFor="password">Password</label><br />
                <input
                    type="password"
                    id="password"
                    name="password"
                    className={`textBox ${errors.password ? 'error' : ''}`}
                    placeholder='password'
                    onChange={(e) => {inputText('password', e.target.value)}}
                />
                {
                    (errors.password) ? <div className='errorText'>
                    {errors.password}
                    </div> : void 0
                }
                <br />

                <label htmlFor="passwordConfirm">Confirm Password</label><br />
                <input
                    type="password"
                    id="passwordConfirm"
                    name="passwordConfirm"
                    className={`textBox ${errors.passwordConfirm ? 'error' : ''}`}
                    placeholder='password'
                    onChange={(e) => {inputText('passwordConfirm', e.target.value)}}
                />
                {
                    (errors.passwordConfirm || errors.general) ? <div className='errorText'>
                    {errors.passwordConfirm ? errors.passwordConfirm : errors.general}
                    </div> : void 0
                }
                <br />
                <br />

                <div className="buttonRow">
                    <input
                        type="submit"
                        value="Create"
                        className='buttonPrimary loginButton'
                    />
                    <a href="/login">
                        <input
                            value="Back to Login"
                            className='buttonSecondary signupButton'
                            readOnly={true}
                        />
                    </a>
                </div>
            </form> 
        </div>
    </div>
}
